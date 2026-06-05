"""Entity-table invariants — guards the single source of truth."""

from __future__ import annotations

from dataclasses import fields

import pytest

from jkbms2mqtt.entities import (
    CELL_STATS_SENSORS,
    FIXED_SENSORS,
    LIVE_BINARY_SENSORS,
    LIVE_SENSORS,
    PACKED_BIT_ENTITIES,
    WRITABLE_ENTITIES,
    Component,
    ReadOnlyEntity,
    all_read_only_entities,
    expand_cell_entities,
    writable_by_command_topic_suffix,
)
from jkbms2mqtt.protocol.jk_modbus import JkRealtime, JkStaticInfo


def _jk_realtime_fields() -> set[str]:
    return {f.name for f in fields(JkRealtime)}


def _jk_static_fields() -> set[str]:
    return {f.name for f in fields(JkStaticInfo)}


class TestSourceFieldsExistOnDataclasses:
    @pytest.mark.parametrize(
        "entity", list(LIVE_SENSORS + LIVE_BINARY_SENSORS + CELL_STATS_SENSORS)
    )
    def test_live_entity_source_field_on_JkRealtime(self, entity: ReadOnlyEntity) -> None:
        assert entity.source_field in _jk_realtime_fields()

    @pytest.mark.parametrize("entity", list(FIXED_SENSORS))
    def test_static_entity_source_field_on_JkStaticInfo(self, entity: ReadOnlyEntity) -> None:
        assert entity.source_field in _jk_static_fields()


class TestUniqueness:
    def test_no_duplicate_object_ids(self) -> None:
        ids: list[str] = []
        for e in LIVE_SENSORS + LIVE_BINARY_SENSORS + CELL_STATS_SENSORS + FIXED_SENSORS:
            ids.append(e.object_id)
        for w in WRITABLE_ENTITIES:
            ids.append(w.object_id)
        for p in PACKED_BIT_ENTITIES:
            ids.append(p.object_id)
        assert len(ids) == len(set(ids))

    def test_no_duplicate_topic_suffixes(self) -> None:
        suffixes: list[str] = []
        for e in LIVE_SENSORS + LIVE_BINARY_SENSORS + CELL_STATS_SENSORS + FIXED_SENSORS:
            suffixes.append(e.topic_suffix)
        for w in WRITABLE_ENTITIES:
            suffixes.append(w.topic_suffix)
        for p in PACKED_BIT_ENTITIES:
            suffixes.append(p.topic_suffix)
        assert len(suffixes) == len(set(suffixes))


class TestNamingCompat:
    def test_total_voltage_topic_matches_legacy(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "total_voltage")
        assert e.topic_suffix == "Total_Voltage_V"

    def test_soc_topic_matches_legacy(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "soc_percentage")
        assert e.topic_suffix == "SOC_percentage"

    def test_mos_temp_topic_matches_legacy(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "mos_temp")
        assert e.topic_suffix == "Mos_temp"


class TestCellExpansion:
    def test_cells_1_through_n(self) -> None:
        cells = expand_cell_entities(8)
        # N voltages + N resistances.
        assert len(cells) == 16
        ids = [c.object_id for c in cells]
        assert ids[:8] == [f"cell_{n}_volt" for n in range(1, 9)]
        assert ids[8:] == [f"cell_{n}_ohm" for n in range(1, 9)]
        assert cells[0].topic_suffix == "Cell_1_volt"
        assert cells[-1].topic_suffix == "Cell_8_ohm"

    def test_zero_cells(self) -> None:
        cells = expand_cell_entities(0)
        assert cells == ()

    def test_all_read_only_includes_cells(self) -> None:
        entities = all_read_only_entities(cell_count=4)
        cell_ids = {e.object_id for e in entities if e.object_id.startswith("cell_")}
        assert "cell_1_volt" in cell_ids
        assert "cell_4_volt" in cell_ids
        assert "cell_5_volt" not in cell_ids
        assert "cell_1_ohm" in cell_ids
        assert "cell_4_ohm" in cell_ids
        assert "cell_5_ohm" not in cell_ids


class TestWritables:
    def test_number_writables_carry_a_numeric_encoding(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import Encoding
        by_id = {w.object_id: w for w in WRITABLE_ENTITIES}
        # Verified against BMS_1 capture: currents are stored as mA (U32_MILLI),
        # not deci-A as the previous table claimed.
        assert by_id["max_charge_current"].register.encoding is Encoding.U32_MILLI
        assert by_id["balance_trigger_voltage"].register.encoding is Encoding.U32_MILLI

    def test_writables_use_control_prefix(self) -> None:
        for w in WRITABLE_ENTITIES:
            assert w.topic_suffix.startswith("control/")
        for p in PACKED_BIT_ENTITIES:
            assert p.topic_suffix.startswith("control/")

    def test_writable_lookup_keyed_by_set_topic(self) -> None:
        lookup = writable_by_command_topic_suffix()
        assert "control/max_charge_current/set" in lookup
        assert "control/smart_sleep_voltage/set" in lookup
        assert "control/smart_sleep_switch/set" in lookup

    def test_writable_lookup_maps_to_the_actual_entities(self) -> None:
        """The router dispatches on these objects, so the values must be the
        exact entities — not None or the wrong one."""
        lookup = writable_by_command_topic_suffix()
        for w in WRITABLE_ENTITIES:
            assert lookup[f"{w.topic_suffix}/set"] is w
        for p in PACKED_BIT_ENTITIES:
            assert lookup[f"{p.topic_suffix}/set"] is p
        # Every entry is keyed off a /set topic and nothing else leaks in.
        assert all(key.endswith("/set") for key in lookup)
        assert len(lookup) == len(WRITABLE_ENTITIES) + len(PACKED_BIT_ENTITIES)


class TestCellEntityFields:
    """Pin every field `expand_cell_entities` builds — these are the entities HA
    renders per cell, and a wrong unit / source_field / category misreports."""

    def test_voltage_entity_all_fields(self) -> None:
        # n=2 so the n-1 index (→ [1]) is distinguishable from n.
        volt = next(c for c in expand_cell_entities(3) if c.object_id == "cell_2_volt")
        assert volt.topic_suffix == "Cell_2_volt"
        assert volt.source_field == "cell_voltages_v[1]"
        assert volt.component is Component.SENSOR
        assert volt.device_class == "voltage"
        assert volt.state_class == "measurement"
        assert volt.unit_of_measurement == "V"
        assert volt.decimals == 3
        assert volt.description == "Cell 2 voltage."
        assert volt.entity_category is None  # per-cell voltage is primary

    def test_resistance_entity_all_fields(self) -> None:
        ohm = next(c for c in expand_cell_entities(3) if c.object_id == "cell_2_ohm")
        assert ohm.topic_suffix == "Cell_2_ohm"
        assert ohm.source_field == "cell_resistances_ohm[1]"
        assert ohm.component is Component.SENSOR
        assert ohm.device_class is None
        assert ohm.state_class == "measurement"
        assert ohm.unit_of_measurement == "Ω"
        assert ohm.decimals == 3
        assert ohm.description == "Cell 2 internal resistance."
        assert ohm.entity_category == "diagnostic"

    def test_source_field_index_is_zero_based(self) -> None:
        cells = {c.object_id: c for c in expand_cell_entities(2)}
        assert cells["cell_1_volt"].source_field == "cell_voltages_v[0]"
        assert cells["cell_1_ohm"].source_field == "cell_resistances_ohm[0]"


class TestWritableConstructors:
    """`_writable_from_register` / `_writable_from_packed_bit` build the writable
    table; assert every field they set."""

    def _reg(self, name, encoding):  # test helper — builds a RegisterDef
        from jkbms2mqtt.protocol.jk_settings import RegisterDef, WriteTier

        return RegisterDef(
            name=name,
            address=0x1000,
            encoding=encoding,
            min_value=0,
            max_value=10,
            step=1,
            unit="A",
            tier=WriteTier.BASIC,
            description=f"{name} description.",
        )

    def test_from_register_numeric(self) -> None:
        from jkbms2mqtt.entities import Component, _writable_from_register
        from jkbms2mqtt.protocol.jk_settings import Encoding

        reg = self._reg("max_charge_current", Encoding.U32_MILLI)
        w = _writable_from_register(reg)
        assert w.object_id == "max_charge_current"
        assert w.topic_suffix == "control/max_charge_current"
        assert w.register is reg
        assert w.component is Component.NUMBER
        assert w.description == "max_charge_current description."
        assert w.entity_category == "config"

    def test_from_register_bool_is_switch(self) -> None:
        from jkbms2mqtt.entities import Component, _writable_from_register
        from jkbms2mqtt.protocol.jk_settings import Encoding

        reg = self._reg("charging_switch", Encoding.BOOL32)
        w = _writable_from_register(reg)
        assert w.component is Component.SWITCH

    def test_from_packed_bit_all_fields(self) -> None:
        from jkbms2mqtt.entities import Component, _writable_from_packed_bit
        from jkbms2mqtt.protocol.jk_settings import PackedBitDef, WriteTier

        bit = PackedBitDef(
            name="smart_sleep_switch",
            register=0x1114,
            bit_mask=0x0002,
            tier=WriteTier.BASIC,
            description="smart sleep.",
        )
        p = _writable_from_packed_bit(bit)
        assert p.object_id == "smart_sleep_switch"
        assert p.topic_suffix == "control/smart_sleep_switch"
        assert p.bit is bit
        assert p.verified is False  # bit positions unconfirmed → hidden by default
        assert p.component is Component.SWITCH
        assert p.entity_category == "config"


class TestEntityCategories:
    """HA `entity_category` invariants — see docs/ENTITIES.md and report
    `https://developers.home-assistant.io/docs/core/entity/#categorizing-entities`.
    """

    def test_every_writable_is_config(self) -> None:
        """Settable thresholds belong in HA's Configuration section."""
        for w in WRITABLE_ENTITIES:
            assert w.entity_category == "config", (
                f"{w.object_id} should be entity_category='config'"
            )

    def test_every_packed_bit_is_config(self) -> None:
        """Device-mode toggles belong in HA's Configuration section."""
        for p in PACKED_BIT_ENTITIES:
            assert p.entity_category == "config", (
                f"{p.object_id} should be entity_category='config'"
            )

    def test_static_info_is_diagnostic(self) -> None:
        """Model / hw / sw / serial belong in HA's Diagnostics section."""
        for e in FIXED_SENSORS:
            assert e.entity_category == "diagnostic", (
                f"{e.object_id} should be entity_category='diagnostic'"
            )

    def test_lifetime_counters_are_diagnostic(self) -> None:
        by_id = {e.object_id: e for e in LIVE_SENSORS}
        for name in ("cycle_count", "total_cycle_capacity_ah", "total_runtime"):
            assert by_id[name].entity_category == "diagnostic", (
                f"{name} should be entity_category='diagnostic'"
            )

    def test_soh_is_diagnostic(self) -> None:
        by_id = {e.object_id: e for e in LIVE_SENSORS}
        assert by_id["soh_percentage"].entity_category == "diagnostic"

    def test_alarm_bits_diagnostic_alarms_primary(self) -> None:
        """`alarm_bits` (raw bitfield) is diagnostic; `alarms` (decoded text)
        is the primary "is there a problem" entity."""
        by_id = {e.object_id: e for e in LIVE_SENSORS}
        assert by_id["alarm_bits"].entity_category == "diagnostic"
        assert by_id["alarms"].entity_category is None

    def test_present_cell_count_is_diagnostic(self) -> None:
        by_id = {e.object_id: e for e in CELL_STATS_SENSORS}
        assert by_id["present_cell_count"].entity_category == "diagnostic"

    def test_per_cell_voltages_primary_resistances_diagnostic(self) -> None:
        from jkbms2mqtt.entities import expand_cell_entities
        cells = expand_cell_entities(2)
        by_id = {e.object_id: e for e in cells}
        assert by_id["cell_1_volt"].entity_category is None
        assert by_id["cell_2_volt"].entity_category is None
        assert by_id["cell_1_ohm"].entity_category == "diagnostic"
        assert by_id["cell_2_ohm"].entity_category == "diagnostic"

    def test_main_primary_sensors_have_no_category(self) -> None:
        """Total V/I/P, SoC, capacities, balance, temps, charge/discharge
        switch states are daily-use → no entity_category."""
        by_id = {e.object_id: e for e in LIVE_SENSORS}
        for name in (
            "total_voltage", "total_current", "total_power",
            "soc_percentage",
            "remaining_capacity_ah", "nominal_capacity_ah",
            "balance_current",
            "mos_temp", "probe_1_temp", "probe_2_temp",
            "probe_3_temp", "probe_4_temp", "probe_5_temp",
        ):
            assert by_id[name].entity_category is None, (
                f"{name} should NOT have entity_category set (primary entity)"
            )

    def test_no_invalid_category_string(self) -> None:
        """HA only accepts None / 'config' / 'diagnostic'."""
        valid = {None, "config", "diagnostic"}
        for e in (
            LIVE_SENSORS + LIVE_BINARY_SENSORS + CELL_STATS_SENSORS + FIXED_SENSORS
        ):
            assert e.entity_category in valid, e.object_id
        for w in WRITABLE_ENTITIES:
            assert w.entity_category in valid, w.object_id
        for p in PACKED_BIT_ENTITIES:
            assert p.entity_category in valid, p.object_id


class TestCoverage:
    def test_critical_fields_have_entities(self) -> None:
        live_sources = {e.source_field for e in LIVE_SENSORS + LIVE_BINARY_SENSORS}
        stat_sources = {e.source_field for e in CELL_STATS_SENSORS}
        all_used = live_sources | stat_sources
        for must_have in (
            "total_voltage_v",
            "total_current_a",
            "soc_percentage",
            "soh_percentage",
            "cycle_count",
            "mos_temp_c",
            "charge_enabled",
            "discharge_enabled",
            "cell_voltage_avg_v",
            "cell_voltage_delta_v",
        ):
            assert must_have in all_used, f"Missing entity for {must_have}"
