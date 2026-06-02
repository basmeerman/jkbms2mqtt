"""Entity-table tests — invariants over the single source of truth."""

from __future__ import annotations

import pytest

from jkbms2mqtt.entities import (
    CELL_STATS_SENSORS,
    FIXED_SENSORS,
    LIVE_BINARY_SENSORS,
    LIVE_SENSORS,
    PACKED_BIT_ENTITIES,
    WRITABLE_ENTITIES,
    Component,
    all_read_only_entities,
    expand_cell_entities,
    writable_by_command_topic_suffix,
)


def test_no_duplicate_object_ids() -> None:
    all_ids: list[str] = []
    for e in LIVE_SENSORS + LIVE_BINARY_SENSORS + CELL_STATS_SENSORS + FIXED_SENSORS:
        all_ids.append(e.object_id)
    for w in WRITABLE_ENTITIES:
        all_ids.append(w.object_id)
    for p in PACKED_BIT_ENTITIES:
        all_ids.append(p.object_id)
    assert len(all_ids) == len(set(all_ids)), f"duplicates: {all_ids}"


def test_no_duplicate_topic_suffixes() -> None:
    suffixes: list[str] = []
    for e in LIVE_SENSORS + LIVE_BINARY_SENSORS + CELL_STATS_SENSORS + FIXED_SENSORS:
        suffixes.append(e.topic_suffix)
    for w in WRITABLE_ENTITIES:
        suffixes.append(w.topic_suffix)
    for p in PACKED_BIT_ENTITIES:
        suffixes.append(p.topic_suffix)
    assert len(suffixes) == len(set(suffixes))


def test_writable_entities_use_v4_control_prefix() -> None:
    for w in WRITABLE_ENTITIES:
        assert w.topic_suffix.startswith("control/")


def test_writable_entities_split_by_component() -> None:
    components = {w.object_id: w.component for w in WRITABLE_ENTITIES}
    # bool32-encoded entries must be switches
    assert components["charging_switch"] is Component.SWITCH
    assert components["balance_switch"] is Component.SWITCH
    # numeric ones must be NUMBER
    assert components["max_charge_current"] is Component.NUMBER
    assert components["balance_trigger_voltage"] is Component.NUMBER


def test_legacy_french_topics_present_where_known() -> None:
    # Spot-check: the ones we explicitly noted in the plan.
    french_renames = {e.topic_suffix: e.legacy_french_topic for e in LIVE_SENSORS}
    assert french_renames["Total_Voltage_V"] == "Tension_Totale_volt"
    assert french_renames["Total_Current_A"] == "Courant_total"
    assert french_renames["SOC_percentage"] == "SOC_pourcentage"


def test_french_rename_for_static_metadata() -> None:
    fixed_renames = {e.topic_suffix: e.legacy_french_topic for e in FIXED_SENSORS}
    assert fixed_renames["manufacturing_date"] == "Date_Fabrication"
    assert fixed_renames["request_charge_voltage_time"] == "Temps_RCV"


def test_expand_cell_entities_creates_two_per_cell() -> None:
    cells = expand_cell_entities(8)
    assert len(cells) == 16  # 8 volt + 8 ohm
    # 1-indexed
    assert cells[0].object_id == "cell_1_volt"
    assert cells[1].object_id == "cell_1_ohm"
    assert cells[-2].object_id == "cell_8_volt"
    assert cells[-1].object_id == "cell_8_ohm"


def test_all_read_only_entities_includes_cell_dependent_set() -> None:
    base = all_read_only_entities(cell_count=4)
    assert any(e.object_id == "cell_4_ohm" for e in base)
    assert not any(e.object_id == "cell_5_volt" for e in base)


def test_packed_bit_entities_use_switch_component() -> None:
    for p in PACKED_BIT_ENTITIES:
        assert p.component is Component.SWITCH


def test_writable_by_command_topic_lookup() -> None:
    lookup = writable_by_command_topic_suffix()
    assert "control/charging_switch/set" in lookup
    assert "control/max_charge_current/set" in lookup
    assert "control/smart_sleep_switch/set" in lookup
    assert "control/balance_trigger_voltage/set" in lookup


def test_writable_legacy_french_is_none() -> None:
    for w in WRITABLE_ENTITIES:
        assert w.legacy_french_topic is None
    for p in PACKED_BIT_ENTITIES:
        assert p.legacy_french_topic is None


def test_every_sensor_has_a_source_field() -> None:
    for e in LIVE_SENSORS + LIVE_BINARY_SENSORS + CELL_STATS_SENSORS + FIXED_SENSORS:
        assert e.source_field, f"{e.object_id}: missing source_field"


@pytest.mark.parametrize("e", list(LIVE_SENSORS + CELL_STATS_SENSORS))
def test_live_sensors_carry_state_class(e: object) -> None:
    from jkbms2mqtt.entities import ReadOnlyEntity

    assert isinstance(e, ReadOnlyEntity)
    # Some sensors (e.g. integer counts) may legitimately have measurement OR total_increasing.
    assert e.state_class in (None, "measurement", "total_increasing")
