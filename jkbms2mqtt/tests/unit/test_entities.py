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
    def test_switch_writables_carry_a_bool_encoding(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import Encoding
        by_id = {w.object_id: w for w in WRITABLE_ENTITIES}
        assert by_id["charging_switch"].register.encoding is Encoding.BOOL32
        assert by_id["balance_switch"].register.encoding is Encoding.BOOL32

    def test_number_writables_carry_a_numeric_encoding(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import Encoding
        by_id = {w.object_id: w for w in WRITABLE_ENTITIES}
        assert by_id["max_charge_current"].register.encoding is Encoding.U32_DECI
        assert by_id["balance_trigger_voltage"].register.encoding is Encoding.U32_MILLI

    def test_writables_use_control_prefix(self) -> None:
        for w in WRITABLE_ENTITIES:
            assert w.topic_suffix.startswith("control/")
        for p in PACKED_BIT_ENTITIES:
            assert p.topic_suffix.startswith("control/")

    def test_writable_lookup_keyed_by_set_topic(self) -> None:
        lookup = writable_by_command_topic_suffix()
        assert "control/charging_switch/set" in lookup
        assert "control/max_charge_current/set" in lookup
        assert "control/smart_sleep_switch/set" in lookup


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
