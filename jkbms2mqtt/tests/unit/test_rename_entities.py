"""Tests for scripts/rename_entities.py — the entity-id migration tool.

The script renames entities on a live Home Assistant, so its planning logic
must be right before it ever connects: a wrong target id silently moves an
entity (and its history) somewhere the dashboard cannot find.

Only the pure parts are exercised here; the websocket plumbing is operator
tooling and is imported lazily so these tests run without that dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from rename_entities import ha_slugify, plan, target_object_ids  # noqa: E402


def _entry(entity_id: str, object_id: str, bms: str = "BMS_1") -> dict:
    return {"entity_id": entity_id, "unique_id": f"{bms}_device_{object_id}"}


class TestHaSlugify:
    def test_lowercases_and_joins_with_underscores(self) -> None:
        assert ha_slugify("Total voltage") == "total_voltage"
        assert ha_slugify("Cell voltage at 100% SoC") == "cell_voltage_at_100_soc"

    def test_strips_punctuation_and_non_ascii(self) -> None:
        assert ha_slugify("Power-off voltage") == "power_off_voltage"
        assert ha_slugify("Short-circuit protection delay") == "short_circuit_protection_delay"


class TestTargets:
    def test_covers_every_published_entity(self) -> None:
        targets = target_object_ids()
        for object_id in ("total_voltage", "max_charge_current", "cell_16_ohm", "last_seen"):
            assert object_id in targets

    def test_domain_and_slug(self) -> None:
        targets = target_object_ids()
        assert targets["total_voltage"] == ("sensor", "total_voltage")
        assert targets["soc_percentage"] == ("sensor", "state_of_charge")
        assert targets["cell_1_ohm"] == ("sensor", "cell_1_resistance")
        # Settings are listed read-only; the registry's own domain wins in plan().
        assert targets["max_charge_current"] == ("sensor", "maximum_charge_current")
        assert targets["charging_switch"] == ("binary_sensor", "charging")


class TestPlan:
    def test_renames_old_style_ids(self) -> None:
        entries = [
            _entry("sensor.bms_1_total_pack_voltage", "total_voltage"),
            _entry("sensor.bms_1_device_max_charge_current", "max_charge_current"),
        ]
        assert plan(entries, target_object_ids()) == [
            ("sensor.bms_1_total_pack_voltage", "sensor.bms_1_total_voltage"),
            ("sensor.bms_1_device_max_charge_current", "sensor.bms_1_maximum_charge_current"),
        ]

    def test_leaves_correct_ids_alone(self) -> None:
        entries = [_entry("sensor.bms_1_total_voltage", "total_voltage")]
        assert plan(entries, target_object_ids()) == []

    def test_keeps_the_registry_domain_for_a_writable_tier(self) -> None:
        """With a write tier on, the setting is a number/switch; the rename must
        stay in that domain instead of moving it to sensor."""
        entries = [_entry("number.bms_1_device_max_charge_current", "max_charge_current")]
        assert plan(entries, target_object_ids()) == [
            ("number.bms_1_device_max_charge_current", "number.bms_1_maximum_charge_current")
        ]

    def test_uses_the_device_name_from_the_unique_id(self) -> None:
        entries = [_entry("sensor.bms_6_total_pack_voltage", "total_voltage", bms="BMS_6")]
        assert plan(entries, target_object_ids()) == [
            ("sensor.bms_6_total_pack_voltage", "sensor.bms_6_total_voltage")
        ]

    def test_ignores_entities_from_other_integrations(self) -> None:
        entries = [
            {"entity_id": "sensor.kitchen_temperature", "unique_id": "zigbee-1234"},
            {"entity_id": "sensor.no_unique_id", "unique_id": None},
            _entry("sensor.bms_1_unknown_thing", "not_a_bridge_entity"),
        ]
        assert plan(entries, target_object_ids()) == []

    def test_skips_a_target_that_is_already_taken(self) -> None:
        """Never clobber an existing entity: HA would refuse, and the operator
        needs to see which one collided."""
        entries = [
            _entry("sensor.bms_1_total_pack_voltage", "total_voltage"),
            {"entity_id": "sensor.bms_1_total_voltage", "unique_id": "something-else"},
        ]
        assert plan(entries, target_object_ids()) == []

    def test_is_idempotent(self) -> None:
        entries = [_entry("sensor.bms_1_total_pack_voltage", "total_voltage")]
        targets = target_object_ids()
        rows = plan(entries, targets)
        renamed = [_entry(rows[0][1], "total_voltage")]
        assert plan(renamed, targets) == []
