"""Declarative entity table — single source of truth for every MQTT entity.

Consumed by:

- ``mqtt.py`` for HA Discovery payload generation
- ``mqtt.py`` for state publishing
- Tests for invariant checks

Each ``ReadOnlyEntity`` maps a JkRealtime / JkStaticInfo attribute to a topic
suffix appended to ``<bms_name>/``. Each ``WritableEntity`` references a
``RegisterDef`` (or ``PackedBitDef``) and inherits its tier from there.

Topic suffixes follow the convention used by the Jean-Luc-style HA add-on
that users may be migrating from (e.g. ``Total_Voltage_V``, ``Cell_1_volt``,
``Mos_temp``) so existing dashboards and automations work unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

from jkbms2mqtt.protocol.jk_settings import (
    BASIC_REGISTERS,
    PACKED_BITS,
    SAFETY_REGISTERS,
    Encoding,
    PackedBitDef,
    RegisterDef,
)


@unique
class Component(str, Enum):
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    NUMBER = "number"
    SWITCH = "switch"


@dataclass(frozen=True, slots=True)
class ReadOnlyEntity:
    """A telemetry entity. ``source_field`` is the attribute name on the decoded
    dataclass (``JkRealtime`` or ``JkStaticInfo``).

    ``decimals`` matches the source resolution of the BMS field:

    - 3 for voltages (1 mV) and currents (1 mA).
    - 1 for temperatures (0.1 °C).
    - 0 for percentages, counts, and seconds.
    - ``None`` for non-numeric entities (binary sensors, strings).

    The value is both used to format the MQTT payload (so we don't publish
    misleading trailing zeros) and surfaced in HA Discovery as
    ``suggested_display_precision`` so the frontend renders at full precision
    instead of falling back to its default 1-decimal rounding.
    """

    object_id: str               # snake_case
    topic_suffix: str            # appended to `<bms_name>/`
    source_field: str
    component: Component
    device_class: str | None
    state_class: str | None
    unit_of_measurement: str | None
    decimals: int | None
    description: str


@dataclass(frozen=True, slots=True)
class WritableEntity:
    """A writable parameter backed by a single 32-bit register (function 0x10)."""

    object_id: str
    topic_suffix: str            # always `control/<name>`
    register: RegisterDef
    component: Component
    description: str


@dataclass(frozen=True, slots=True)
class PackedBitEntity:
    """A writable boolean stored as one bit inside the packed register 0x1114."""

    object_id: str
    topic_suffix: str            # always `control/<name>`
    bit: PackedBitDef
    component: Component = Component.SWITCH


# -- Read-only sensors from JkRealtime -------------------------------------------------

LIVE_SENSORS: Final[tuple[ReadOnlyEntity, ...]] = (
    ReadOnlyEntity(
        object_id="total_voltage",
        topic_suffix="Total_Voltage_V",
        source_field="total_voltage_v",
        component=Component.SENSOR,
        device_class="voltage",
        state_class="measurement",
        unit_of_measurement="V",
        decimals=3,
        description="Total pack voltage.",
    ),
    ReadOnlyEntity(
        object_id="total_current",
        topic_suffix="Total_Current_A",
        source_field="total_current_a",
        component=Component.SENSOR,
        device_class="current",
        state_class="measurement",
        unit_of_measurement="A",
        decimals=3,
        description="Total pack current (negative = discharge).",
    ),
    ReadOnlyEntity(
        object_id="total_power",
        topic_suffix="Total_Power_W",
        source_field="total_power_w",
        component=Component.SENSOR,
        device_class="power",
        state_class="measurement",
        unit_of_measurement="W",
        decimals=1,
        description="Total pack power (signed).",
    ),
    ReadOnlyEntity(
        object_id="soc_percentage",
        topic_suffix="SOC_percentage",
        source_field="soc_percentage",
        component=Component.SENSOR,
        device_class="battery",
        state_class="measurement",
        unit_of_measurement="%",
        decimals=0,
        description="State of charge.",
    ),
    ReadOnlyEntity(
        object_id="soh_percentage",
        topic_suffix="SOH_percentage",
        source_field="soh_percentage",
        component=Component.SENSOR,
        device_class=None,
        state_class="measurement",
        unit_of_measurement="%",
        decimals=0,
        description="State of health.",
    ),
    ReadOnlyEntity(
        object_id="remaining_capacity_ah",
        topic_suffix="Remaining_Capacity_Ah",
        source_field="remaining_capacity_ah",
        component=Component.SENSOR,
        device_class=None,
        state_class="measurement",
        unit_of_measurement="Ah",
        decimals=2,
        description="Remaining battery capacity.",
    ),
    ReadOnlyEntity(
        object_id="nominal_capacity_ah",
        topic_suffix="Battery_Capacity_Ah",
        source_field="nominal_capacity_ah",
        component=Component.SENSOR,
        device_class=None,
        state_class="measurement",
        unit_of_measurement="Ah",
        decimals=2,
        description="Nominal pack capacity.",
    ),
    ReadOnlyEntity(
        object_id="cycle_count",
        topic_suffix="Cycle_Count",
        source_field="cycle_count",
        component=Component.SENSOR,
        device_class=None,
        state_class="total_increasing",
        unit_of_measurement=None,
        decimals=0,
        description="Charge cycle count.",
    ),
    ReadOnlyEntity(
        object_id="balance_current",
        topic_suffix="Balance_current",
        source_field="balance_current_a",
        component=Component.SENSOR,
        device_class="current",
        state_class="measurement",
        unit_of_measurement="A",
        decimals=3,
        description="Cell-balance current.",
    ),
    ReadOnlyEntity(
        object_id="mos_temp",
        topic_suffix="Mos_temp",
        source_field="mos_temp_c",
        component=Component.SENSOR,
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        decimals=1,
        description="MOSFET temperature.",
    ),
    ReadOnlyEntity(
        object_id="probe_1_temp",
        topic_suffix="Probe_1_temp",
        source_field="probe_1_temp_c",
        component=Component.SENSOR,
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        decimals=1,
        description="Probe 1 temperature.",
    ),
    ReadOnlyEntity(
        object_id="probe_2_temp",
        topic_suffix="Probe_2_temp",
        source_field="probe_2_temp_c",
        component=Component.SENSOR,
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        decimals=1,
        description="Probe 2 temperature.",
    ),
    ReadOnlyEntity(
        object_id="probe_3_temp",
        topic_suffix="Probe_3_temp",
        source_field="probe_3_temp_c",
        component=Component.SENSOR,
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        decimals=1,
        description="Probe 3 temperature.",
    ),
    ReadOnlyEntity(
        object_id="probe_4_temp",
        topic_suffix="Probe_4_temp",
        source_field="probe_4_temp_c",
        component=Component.SENSOR,
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        decimals=1,
        description="Probe 4 temperature.",
    ),
    ReadOnlyEntity(
        object_id="probe_5_temp",
        topic_suffix="Probe_5_temp",
        source_field="probe_5_temp_c",
        component=Component.SENSOR,
        device_class="temperature",
        state_class="measurement",
        unit_of_measurement="°C",
        decimals=1,
        description="Probe 5 temperature.",
    ),
    ReadOnlyEntity(
        object_id="total_runtime",
        topic_suffix="Total_runtime",
        source_field="runtime_s",
        component=Component.SENSOR,
        device_class="duration",
        state_class="total_increasing",
        unit_of_measurement="s",
        decimals=0,
        description="Total runtime since BMS power-on.",
    ),
)


# -- Binary sensors -------------------------------------------------------------------

LIVE_BINARY_SENSORS: Final[tuple[ReadOnlyEntity, ...]] = (
    ReadOnlyEntity(
        object_id="switch_charge",
        topic_suffix="Switch_Charge",
        source_field="charge_enabled",
        component=Component.BINARY_SENSOR,
        device_class="power",
        state_class=None,
        unit_of_measurement=None,
        decimals=None,
        description="Charge MOSFET state (reported).",
    ),
    ReadOnlyEntity(
        object_id="switch_discharge",
        topic_suffix="Switch_Discharge",
        source_field="discharge_enabled",
        component=Component.BINARY_SENSOR,
        device_class="power",
        state_class=None,
        unit_of_measurement=None,
        decimals=None,
        description="Discharge MOSFET state (reported).",
    ),
    ReadOnlyEntity(
        object_id="switch_balance",
        topic_suffix="Switch_Balance",
        source_field="balance_active",
        component=Component.BINARY_SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        decimals=None,
        description="Balance state (reported).",
    ),
)


# -- Cell-statistics sensors derived from JkRealtime ----------------------------------

CELL_STATS_SENSORS: Final[tuple[ReadOnlyEntity, ...]] = (
    ReadOnlyEntity(
        object_id="cell_voltage_average",
        topic_suffix="cell_voltage_average",
        source_field="cell_voltage_avg_v",
        component=Component.SENSOR,
        device_class="voltage",
        state_class="measurement",
        unit_of_measurement="V",
        decimals=3,
        description="Average cell voltage (populated cells only).",
    ),
    ReadOnlyEntity(
        object_id="cell_voltage_delta",
        topic_suffix="cell_voltage_delta",
        source_field="cell_voltage_delta_v",
        component=Component.SENSOR,
        device_class="voltage",
        state_class="measurement",
        unit_of_measurement="V",
        decimals=3,
        description="Cell voltage delta (max − min).",
    ),
    ReadOnlyEntity(
        object_id="cell_voltage_max_value",
        topic_suffix="cell_voltage_max_value",
        source_field="cell_voltage_max_v",
        component=Component.SENSOR,
        device_class="voltage",
        state_class="measurement",
        unit_of_measurement="V",
        decimals=3,
        description="Highest cell voltage.",
    ),
    ReadOnlyEntity(
        object_id="cell_voltage_min_value",
        topic_suffix="cell_voltage_min_value",
        source_field="cell_voltage_min_v",
        component=Component.SENSOR,
        device_class="voltage",
        state_class="measurement",
        unit_of_measurement="V",
        decimals=3,
        description="Lowest cell voltage.",
    ),
    ReadOnlyEntity(
        object_id="cell_voltage_max_number",
        topic_suffix="cell_voltage_max_number",
        source_field="cell_voltage_max_number",
        component=Component.SENSOR,
        device_class=None,
        state_class="measurement",
        unit_of_measurement=None,
        decimals=0,
        description="1-indexed cell number with the highest voltage.",
    ),
    ReadOnlyEntity(
        object_id="cell_voltage_min_number",
        topic_suffix="cell_voltage_min_number",
        source_field="cell_voltage_min_number",
        component=Component.SENSOR,
        device_class=None,
        state_class="measurement",
        unit_of_measurement=None,
        decimals=0,
        description="1-indexed cell number with the lowest voltage.",
    ),
    ReadOnlyEntity(
        # Renamed from `cell_count` to avoid clashing with the writable
        # `cell_count` setting (which is the user-configured cell count).
        object_id="present_cell_count",
        topic_suffix="present_cell_count",
        source_field="cell_count",
        component=Component.SENSOR,
        device_class=None,
        state_class="measurement",
        unit_of_measurement=None,
        decimals=0,
        description="Number of cells the BMS reports as present.",
    ),
)


# -- Per-cell entities (1-indexed) ----------------------------------------------------


def expand_cell_entities(cell_count: int) -> tuple[ReadOnlyEntity, ...]:
    """Return one voltage entity per populated cell."""
    out: list[ReadOnlyEntity] = []
    for n in range(1, cell_count + 1):
        out.append(
            ReadOnlyEntity(
                object_id=f"cell_{n}_volt",
                topic_suffix=f"Cell_{n}_volt",
                source_field=f"cell_voltages_v[{n - 1}]",
                component=Component.SENSOR,
                device_class="voltage",
                state_class="measurement",
                unit_of_measurement="V",
                decimals=3,
                description=f"Cell {n} voltage.",
            )
        )
    return tuple(out)


# -- Static-info sensors from JkStaticInfo --------------------------------------------

FIXED_SENSORS: Final[tuple[ReadOnlyEntity, ...]] = (
    ReadOnlyEntity(
        object_id="bms_model",
        topic_suffix="bms",
        source_field="model",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        decimals=None,
        description="BMS model identifier.",
    ),
    ReadOnlyEntity(
        object_id="hw_version",
        topic_suffix="fw",
        source_field="hw_version",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        decimals=None,
        description="BMS hardware version.",
    ),
    ReadOnlyEntity(
        object_id="sw_version",
        topic_suffix="sw",
        source_field="sw_version",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        decimals=None,
        description="BMS software / firmware version.",
    ),
    ReadOnlyEntity(
        object_id="serial_number",
        topic_suffix="serialnb",
        source_field="serial_number",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        decimals=None,
        description="BMS serial number.",
    ),
)


# -- Writable entities -----------------------------------------------------------------


def _writable_from_register(reg: RegisterDef) -> WritableEntity:
    component = Component.SWITCH if reg.encoding is Encoding.BOOL32 else Component.NUMBER
    return WritableEntity(
        object_id=reg.name,
        topic_suffix=f"control/{reg.name}",
        register=reg,
        component=component,
        description=reg.description,
    )


def _writable_from_packed_bit(bit: PackedBitDef) -> PackedBitEntity:
    return PackedBitEntity(
        object_id=bit.name,
        topic_suffix=f"control/{bit.name}",
        bit=bit,
    )


WRITABLE_ENTITIES: Final[tuple[WritableEntity, ...]] = tuple(
    _writable_from_register(r) for r in BASIC_REGISTERS + SAFETY_REGISTERS
)

PACKED_BIT_ENTITIES: Final[tuple[PackedBitEntity, ...]] = tuple(
    _writable_from_packed_bit(b) for b in PACKED_BITS
)


def all_read_only_entities(cell_count: int) -> tuple[ReadOnlyEntity, ...]:
    """Return every read-only entity, expanded for the given cell_count."""
    return (
        LIVE_SENSORS
        + LIVE_BINARY_SENSORS
        + CELL_STATS_SENSORS
        + expand_cell_entities(cell_count)
        + FIXED_SENSORS
    )


def writable_by_command_topic_suffix() -> dict[str, WritableEntity | PackedBitEntity]:
    """Lookup table: ``control/<name>/set`` → entity, for the MQTT write router."""
    out: dict[str, WritableEntity | PackedBitEntity] = {}
    for w in WRITABLE_ENTITIES:
        out[f"{w.topic_suffix}/set"] = w
    for p in PACKED_BIT_ENTITIES:
        out[f"{p.topic_suffix}/set"] = p
    return out
