"""Declarative entity table — the single source of truth for every MQTT entity.

This is consumed by:
- `mqtt.py` for HA Discovery payload generation
- `mqtt.py` for state publishing on decoded frames
- the README and `MIGRATION.md` for the rename mapping
- the test suite for "every entity has unit/class/scale" guard tests

Each `ReadOnlyEntity` maps a field in a decoded `LiveData` / `SetupData` / `FixedData`
object to its MQTT topic suffix. Each `WritableEntity` references a
`RegisterDef` (or `PackedBitDef`) and inherits its tier from there.

The `legacy_french_topic` field records an older French topic name for users
porting from a French-locale solution; see `MIGRATION.md` for the rewrite map.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

from jkbms2mqtt.protocol.registers import (
    BASIC_REGISTERS,
    PACKED_BITS,
    SAFETY_REGISTERS,
    PackedBitDef,
    RegisterDef,
)


@unique
class Component(str, Enum):
    """The HA Discovery component types we use."""

    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    NUMBER = "number"
    SWITCH = "switch"
    SELECT = "select"


@dataclass(frozen=True, slots=True)
class ReadOnlyEntity:
    """A telemetry entity: BMS data is decoded and published, no writes."""

    object_id: str  # snake_case HA discovery object_id
    topic_suffix: str  # appended to `<bms_name>/`
    source_field: str  # attribute name on LiveData / SetupData / FixedData
    component: Component
    device_class: str | None
    state_class: str | None
    unit_of_measurement: str | None
    legacy_french_topic: str | None
    description: str


@dataclass(frozen=True, slots=True)
class WritableEntity:
    """A writable entity backed by a single register (function 0x10)."""

    object_id: str
    topic_suffix: str  # always `control/<param>`
    register: RegisterDef
    component: Component
    description: str

    @property
    def legacy_french_topic(self) -> str | None:
        return None  # writable entities never had French names in v3.x


@dataclass(frozen=True, slots=True)
class PackedBitEntity:
    """A writable boolean stored as one bit inside the packed register 0x1114."""

    object_id: str
    topic_suffix: str
    bit: PackedBitDef
    component: Component = Component.SWITCH

    @property
    def legacy_french_topic(self) -> str | None:
        return None


# ---------------------------------------------------------------------------------------------
# Read-only sensor entities (Trame 3 — live data)
# ---------------------------------------------------------------------------------------------

LIVE_SENSORS: Final[tuple[ReadOnlyEntity, ...]] = (
    ReadOnlyEntity(
        object_id="total_voltage",
        topic_suffix="Total_Voltage_V",
        source_field="total_voltage_v",
        component=Component.SENSOR,
        device_class="voltage",
        state_class="measurement",
        unit_of_measurement="V",
        legacy_french_topic="Tension_Totale_volt",
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
        legacy_french_topic="Courant_total",
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
        legacy_french_topic="Puissance_Totale",
        description="Total pack power.",
    ),
    ReadOnlyEntity(
        object_id="soc_percentage",
        topic_suffix="SOC_percentage",
        source_field="soc_percentage",
        component=Component.SENSOR,
        device_class="battery",
        state_class="measurement",
        unit_of_measurement="%",
        legacy_french_topic="SOC_pourcentage",
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
        legacy_french_topic="SOH_pourcentage",
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
        legacy_french_topic="Capacite_restante_Ah",
        description="Remaining battery capacity.",
    ),
    ReadOnlyEntity(
        object_id="battery_capacity_ah",
        topic_suffix="Battery_Capacity_Ah",
        source_field="battery_capacity_ah",
        component=Component.SENSOR,
        device_class=None,
        state_class="measurement",
        unit_of_measurement="Ah",
        legacy_french_topic="Capacite_batterie_Ah",
        description="Reported full battery capacity.",
    ),
    ReadOnlyEntity(
        object_id="cycle_count",
        topic_suffix="Cycle_Count",
        source_field="cycle_count",
        component=Component.SENSOR,
        device_class=None,
        state_class="total_increasing",
        unit_of_measurement=None,
        legacy_french_topic="Nombre_Cycle",
        description="Charge cycle count.",
    ),
    ReadOnlyEntity(
        object_id="cycle_capacity_ah",
        topic_suffix="Cycle_Capacity_Ah",
        source_field="cycle_capacity_ah",
        component=Component.SENSOR,
        device_class=None,
        state_class="total_increasing",
        unit_of_measurement="Ah",
        legacy_french_topic="Cycle_Capacite_Ah",
        description="Total cycle capacity.",
    ),
    ReadOnlyEntity(
        object_id="balance_current",
        topic_suffix="Balance_current",
        source_field="balance_current_a",
        component=Component.SENSOR,
        device_class="current",
        state_class="measurement",
        unit_of_measurement="A",
        legacy_french_topic="Balance_courant",
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
        legacy_french_topic="Mos_temp",
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
        legacy_french_topic="Sonde_1_temp",
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
        legacy_french_topic="Sonde_2_temp",
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
        legacy_french_topic="Sonde_3_temp",
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
        legacy_french_topic="Sonde_4_temp",
        description="Probe 4 temperature.",
    ),
    ReadOnlyEntity(
        object_id="total_runtime",
        topic_suffix="Total_runtime",
        source_field="total_runtime_s",
        component=Component.SENSOR,
        device_class="duration",
        state_class="total_increasing",
        unit_of_measurement="s",
        legacy_french_topic="Total_runtime",
        description="Total runtime since BMS power-on.",
    ),
    ReadOnlyEntity(
        object_id="charge_status",
        topic_suffix="charge_status",
        source_field="charge_status",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="charge_status",
        description="Charge stage code (0=Bulk, 1=Float, 2=Other).",
    ),
    ReadOnlyEntity(
        object_id="charge_status_time",
        topic_suffix="charge_status_time",
        source_field="charge_status_time_s",
        component=Component.SENSOR,
        device_class="duration",
        state_class="total_increasing",
        unit_of_measurement="s",
        legacy_french_topic="charge_status_time",
        description="Time in the current charge stage.",
    ),
    ReadOnlyEntity(
        object_id="heating_current",
        topic_suffix="Heating_Current",
        source_field="heating_current_a",
        component=Component.SENSOR,
        device_class="current",
        state_class="measurement",
        unit_of_measurement="A",
        legacy_french_topic="Chauffage_courant",
        description="Heating element current.",
    ),
)


# Binary-sensor entities — boolean live data
LIVE_BINARY_SENSORS: Final[tuple[ReadOnlyEntity, ...]] = (
    ReadOnlyEntity(
        object_id="switch_charge",
        topic_suffix="Switch_Charge",
        source_field="switch_charge",
        component=Component.BINARY_SENSOR,
        device_class="power",
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="Switch_Charge",
        description="Charge MOSFET status (reported).",
    ),
    ReadOnlyEntity(
        object_id="switch_discharge",
        topic_suffix="Switch_Discharge",
        source_field="switch_discharge",
        component=Component.BINARY_SENSOR,
        device_class="power",
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="Switch_Decharge",
        description="Discharge MOSFET status (reported).",
    ),
    ReadOnlyEntity(
        object_id="switch_balance",
        topic_suffix="Switch_Balance",
        source_field="switch_balance",
        component=Component.BINARY_SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="Switch_Balance",
        description="Balance state (reported).",
    ),
    ReadOnlyEntity(
        object_id="heating",
        topic_suffix="Heating",
        source_field="heating",
        component=Component.BINARY_SENSOR,
        device_class="heat",
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="Chauffage",
        description="Heating element on/off.",
    ),
)


# Per-cell entities — these are dynamic per cell_count and assembled at runtime,
# but their template is declared here for completeness.
CELL_VOLTAGE_TEMPLATE: Final = ReadOnlyEntity(
    object_id="cell_{N}_volt",
    topic_suffix="Cell_{N}_volt",
    source_field="cell_voltages_v[{i}]",  # i is 0-indexed
    component=Component.SENSOR,
    device_class="voltage",
    state_class="measurement",
    unit_of_measurement="V",
    legacy_french_topic="Cell_{N}_volt",
    description="Cell {N} voltage.",
)

CELL_RESISTANCE_TEMPLATE: Final = ReadOnlyEntity(
    object_id="cell_{N}_ohm",
    topic_suffix="Cell_{N}_ohm",
    source_field="cell_resistances_ohm[{i}]",
    component=Component.SENSOR,
    device_class=None,
    state_class="measurement",
    unit_of_measurement="Ω",
    legacy_french_topic="Cell_{N}_ohm",
    description="Cell {N} internal resistance.",
)


def expand_cell_entities(cell_count: int) -> tuple[ReadOnlyEntity, ...]:
    """Return one voltage + one resistance entity for each cell (1-indexed)."""
    out: list[ReadOnlyEntity] = []
    for n in range(1, cell_count + 1):
        i = n - 1
        out.append(
            ReadOnlyEntity(
                object_id=f"cell_{n}_volt",
                topic_suffix=f"Cell_{n}_volt",
                source_field=f"cell_voltages_v[{i}]",
                component=Component.SENSOR,
                device_class="voltage",
                state_class="measurement",
                unit_of_measurement="V",
                legacy_french_topic=f"Cell_{n}_volt",
                description=f"Cell {n} voltage.",
            )
        )
        out.append(
            ReadOnlyEntity(
                object_id=f"cell_{n}_ohm",
                topic_suffix=f"Cell_{n}_ohm",
                source_field=f"cell_resistances_ohm[{i}]",
                component=Component.SENSOR,
                device_class=None,
                state_class="measurement",
                unit_of_measurement="Ω",
                legacy_french_topic=f"Cell_{n}_ohm",
                description=f"Cell {n} internal resistance.",
            )
        )
    return tuple(out)


# Cell-statistics sensors derived from the live frame
CELL_STATS_SENSORS: Final[tuple[ReadOnlyEntity, ...]] = (
    ReadOnlyEntity(
        object_id="cell_voltage_average",
        topic_suffix="cell_voltage_average",
        source_field="cell_voltage_average_v",
        component=Component.SENSOR,
        device_class="voltage",
        state_class="measurement",
        unit_of_measurement="V",
        legacy_french_topic="cell_voltage_average",
        description="Average cell voltage (over populated cells only).",
    ),
    ReadOnlyEntity(
        object_id="cell_voltage_delta",
        topic_suffix="cell_voltage_delta",
        source_field="cell_voltage_delta_v",
        component=Component.SENSOR,
        device_class="voltage",
        state_class="measurement",
        unit_of_measurement="V",
        legacy_french_topic="cell_voltage_delta",
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
        legacy_french_topic="cell_voltage_max_value",
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
        legacy_french_topic="cell_voltage_min_value",
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
        legacy_french_topic="cell_voltage_max_number",
        description="Index (1-based) of the highest-voltage cell.",
    ),
    ReadOnlyEntity(
        object_id="cell_voltage_min_number",
        topic_suffix="cell_voltage_min_number",
        source_field="cell_voltage_min_number",
        component=Component.SENSOR,
        device_class=None,
        state_class="measurement",
        unit_of_measurement=None,
        legacy_french_topic="cell_voltage_min_number",
        description="Index (1-based) of the lowest-voltage cell.",
    ),
)


# Static / fixed-info sensors (Trame 1)
FIXED_SENSORS: Final[tuple[ReadOnlyEntity, ...]] = (
    ReadOnlyEntity(
        object_id="bms_model",
        topic_suffix="bms",
        source_field="bms_model",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="bms",
        description="BMS model identifier.",
    ),
    ReadOnlyEntity(
        object_id="firmware_version",
        topic_suffix="fw",
        source_field="firmware_version",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="fw",
        description="BMS firmware version.",
    ),
    ReadOnlyEntity(
        object_id="software_version",
        topic_suffix="sw",
        source_field="software_version",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="sw",
        description="BMS software version.",
    ),
    ReadOnlyEntity(
        object_id="uptime",
        topic_suffix="uptime",
        source_field="uptime_s",
        component=Component.SENSOR,
        device_class="duration",
        state_class="total_increasing",
        unit_of_measurement="s",
        legacy_french_topic="uptime",
        description="BMS uptime since power-on.",
    ),
    ReadOnlyEntity(
        object_id="power_on_count",
        topic_suffix="power_count",
        source_field="power_on_count",
        component=Component.SENSOR,
        device_class=None,
        state_class="total_increasing",
        unit_of_measurement=None,
        legacy_french_topic="power_count",
        description="Number of times the BMS has been powered on.",
    ),
    ReadOnlyEntity(
        object_id="serial_number",
        topic_suffix="serialnb",
        source_field="serial_number",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="serialnb",
        description="BMS serial number.",
    ),
    ReadOnlyEntity(
        object_id="brand",
        topic_suffix="brand",
        source_field="brand",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="brand",
        description="BMS brand.",
    ),
    ReadOnlyEntity(
        object_id="manufacturing_date",
        topic_suffix="manufacturing_date",
        source_field="manufacturing_date",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="Date_Fabrication",
        description="Manufacturing date (YYMMDD).",
    ),
    ReadOnlyEntity(
        object_id="uart1_protocol_number",
        topic_suffix="uart1_protocol_number",
        source_field="uart1_protocol_number",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="uart1_protocol_number",
        description="UART1 protocol number.",
    ),
    ReadOnlyEntity(
        object_id="can_protocol_number",
        topic_suffix="can_protocol_number",
        source_field="can_protocol_number",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="can_protocol_number",
        description="CAN protocol number.",
    ),
    ReadOnlyEntity(
        object_id="lcd_buzzer_trigger",
        topic_suffix="lcd_buzzer_trigger",
        source_field="lcd_buzzer_trigger",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        legacy_french_topic="lcd_buzzer_trigger",
        description="LCD buzzer trigger source.",
    ),
    ReadOnlyEntity(
        object_id="lcd_buzzer_trigger_value",
        topic_suffix="lcd_buzzer_trigger_value",
        source_field="lcd_buzzer_trigger_value",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement="%",
        legacy_french_topic="lcd_buzzer_trigger_value",
        description="LCD buzzer ON threshold.",
    ),
    ReadOnlyEntity(
        object_id="lcd_buzzer_release_value",
        topic_suffix="lcd_buzzer_release_value",
        source_field="lcd_buzzer_release_value",
        component=Component.SENSOR,
        device_class=None,
        state_class=None,
        unit_of_measurement="%",
        legacy_french_topic="lcd_buzzer_release_value",
        description="LCD buzzer OFF threshold.",
    ),
    ReadOnlyEntity(
        object_id="request_charge_voltage_time",
        topic_suffix="request_charge_voltage_time",
        source_field="request_charge_voltage_time_h",
        component=Component.SENSOR,
        device_class="duration",
        state_class=None,
        unit_of_measurement="h",
        legacy_french_topic="Temps_RCV",
        description="Charge-stage duration request.",
    ),
    ReadOnlyEntity(
        object_id="request_float_voltage_time",
        topic_suffix="request_float_voltage_time",
        source_field="request_float_voltage_time_h",
        component=Component.SENSOR,
        device_class="duration",
        state_class=None,
        unit_of_measurement="h",
        legacy_french_topic="Temps_RFV",
        description="Float-stage duration request.",
    ),
)


# ---------------------------------------------------------------------------------------------
# Writable entities — derived from the register table
# ---------------------------------------------------------------------------------------------


def _writable_from_register(reg: RegisterDef) -> WritableEntity:
    component = (
        Component.SWITCH
        if reg.encoding.value == "bool32"
        else Component.NUMBER
    )
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
    """Return every read-only entity for the given pack `cell_count`."""
    return (
        LIVE_SENSORS
        + LIVE_BINARY_SENSORS
        + CELL_STATS_SENSORS
        + expand_cell_entities(cell_count)
        + FIXED_SENSORS
    )


# Convenient lookup table: topic suffix → entity, used by the MQTT write router.
def writable_by_command_topic_suffix() -> dict[str, WritableEntity | PackedBitEntity]:
    out: dict[str, WritableEntity | PackedBitEntity] = {}
    for e in WRITABLE_ENTITIES:
        out[f"{e.topic_suffix}/set"] = e
    for e in PACKED_BIT_ENTITIES:
        out[f"{e.topic_suffix}/set"] = e
    return out
