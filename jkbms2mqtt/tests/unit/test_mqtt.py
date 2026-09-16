"""HA Discovery + state-message publisher tests."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from jkbms2mqtt.config import Settings, Transport
from jkbms2mqtt.entities import (
    LIVE_SENSORS,
    PACKED_BIT_ENTITIES,
    WRITABLE_ENTITIES,
)
from jkbms2mqtt.mqtt import (
    BRIDGE_AVAILABILITY_TOPIC,
    DiscoveryMessage,
    _format,
    build_discovery_messages,
    discovery_for_packed_bit,
    discovery_for_read_only,
    discovery_for_writable,
    orphan_removals,
    render,
    state_message_last_seen,
    state_messages_from_live,
    state_messages_from_settings,
    state_messages_from_static,
)
from jkbms2mqtt.protocol.jk_modbus import JkRealtime, JkStaticInfo


def _settings(**overrides) -> Settings:
    base = {"transport": Transport.TCP_GATEWAY, "gateway_host": "x.x.x.x", "gateway_port": 502}
    base.update(overrides)
    return Settings(**base)


def _sample_realtime(*, cell_count: int = 16) -> JkRealtime:
    cells = tuple(3.300 + i / 1000 for i in range(cell_count))
    resistances = tuple(0.000 + i / 1000 for i in range(cell_count))
    return JkRealtime(
        cell_voltages_v=cells,
        cell_resistances_ohm=resistances,
        cell_voltage_avg_v=sum(cells) / cell_count if cells else 0.0,
        cell_voltage_delta_v=(cells[-1] - cells[0]) if cells else 0.0,
        cell_voltage_max_v=cells[-1] if cells else 0.0,
        cell_voltage_min_v=cells[0] if cells else 0.0,
        cell_voltage_max_number=cell_count if cells else 0,
        cell_voltage_min_number=1 if cells else 0,
        cell_count=cell_count,
        total_voltage_v=53.0,
        total_current_a=10.0,
        total_power_w=530.0,
        mos_temp_c=25.0,
        probe_1_temp_c=24.0,
        probe_2_temp_c=24.5,
        probe_3_temp_c=24.0,
        probe_4_temp_c=24.0,
        probe_5_temp_c=24.0,
        balance_current_a=0.0,
        balance_active=False,
        soc_percentage=75,
        soh_percentage=100,
        remaining_capacity_ah=80.0,
        nominal_capacity_ah=100.0,
        cycle_count=42,
        total_cycle_capacity_ah=420.0,
        runtime_s=86400,
        charge_enabled=True,
        discharge_enabled=True,
        heating_active=False,
        heating_current_a=0.0,
        alarm_bits=0,
        alarms=(),
        alarms_csv="",
    )


def _sample_static() -> JkStaticInfo:
    return JkStaticInfo(
        model="JK-PB2A16S15P",
        hw_version="HW10A20H",
        sw_version="SW1209HE",
        serial_number="JK202401012345",
    )


# -- Discovery messages ---------------------------------------------------------------


def _config_topics(msgs: list[DiscoveryMessage]) -> list[str]:
    """Topics that carry a config (removals excluded)."""
    return [m.topic for m in msgs if m.payload is not None]


def _removal_topics(msgs: list[DiscoveryMessage]) -> set[str]:
    return {m.topic for m in msgs if m.payload is None}


class TestReadOnlyFallbackCategory:
    """HA rejects entity_category=config on sensor / binary_sensor (issue #16)."""

    @pytest.mark.parametrize("debug", [False, True])
    def test_no_read_only_payload_is_config_when_tiers_off(self, debug: bool) -> None:
        s = _settings(debug_unverified_fields=debug)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        offenders = [
            m.topic
            for m in msgs
            if m.payload is not None
            and ("/sensor/" in m.topic or "/binary_sensor/" in m.topic)
            and m.payload.get("entity_category") == "config"
        ]
        assert offenders == []

    def test_controls_keep_config_when_tiers_on(self) -> None:
        s = _settings(
            enable_basic_writes=True, enable_safety_writes=True, debug_unverified_fields=True
        )
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        controls = [
            m for m in msgs
            if m.payload is not None and ("/number/" in m.topic or "/switch/" in m.topic)
        ]
        assert len(controls) == len(WRITABLE_ENTITIES) + len(PACKED_BIT_ENTITIES)
        assert all(m.payload["entity_category"] == "config" for m in controls)


class TestStaleDiscoveryRemoval:
    """Retained configs a previous run may have left behind get cleared (issue #17)."""

    def test_tier_off_publishes_sensor_and_removes_number(self) -> None:
        msgs = build_discovery_messages(settings=_settings(), bms_name="BMS_1", cell_count=16)
        assert "homeassistant/sensor/BMS_1_device_max_charge_current/config" in (
            _config_topics(msgs)
        )
        assert "homeassistant/number/BMS_1_device_max_charge_current/config" in (
            _removal_topics(msgs)
        )

    def test_tier_on_publishes_number_and_removes_sensor(self) -> None:
        s = _settings(enable_safety_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        assert "homeassistant/number/BMS_1_device_max_charge_current/config" in (
            _config_topics(msgs)
        )
        assert "homeassistant/sensor/BMS_1_device_max_charge_current/config" in (
            _removal_topics(msgs)
        )

    @pytest.mark.parametrize("tiers_on", [False, True])
    def test_bool_writables_remove_other_component(self, tiers_on: bool) -> None:
        s = _settings(enable_basic_writes=tiers_on)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        live, stale = ("switch", "binary_sensor") if tiers_on else ("binary_sensor", "switch")
        assert f"homeassistant/{live}/BMS_1_device_charging_switch/config" in _config_topics(msgs)
        assert f"homeassistant/{stale}/BMS_1_device_charging_switch/config" in (
            _removal_topics(msgs)
        )

    @pytest.mark.parametrize("tiers_on", [False, True])
    def test_every_writable_has_exactly_one_config_and_one_removal(self, tiers_on: bool) -> None:
        s = _settings(
            enable_basic_writes=tiers_on, enable_safety_writes=tiers_on,
            debug_unverified_fields=True,
        )
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        for object_id in (
            *(w.object_id for w in WRITABLE_ENTITIES),
            *(p.object_id for p in PACKED_BIT_ENTITIES),
        ):
            suffix = f"/BMS_1_device_{object_id}/config"
            assert sum(t.endswith(suffix) for t in _config_topics(msgs)) == 1
            assert sum(t.endswith(suffix) for t in _removal_topics(msgs)) == 1

    def test_removal_never_targets_a_published_config(self) -> None:
        for s in (_settings(), _settings(enable_basic_writes=True, enable_safety_writes=True)):
            for debug in (False, True):
                msgs = build_discovery_messages(
                    settings=s.model_copy(update={"debug_unverified_fields": debug}),
                    bms_name="BMS_1", cell_count=13,
                )
                assert not set(_config_topics(msgs)) & _removal_topics(msgs)

    def test_cells_above_count_are_removed(self) -> None:
        msgs = build_discovery_messages(settings=_settings(), bms_name="BMS_1", cell_count=13)
        configs, removals = _config_topics(msgs), _removal_topics(msgs)
        assert "homeassistant/sensor/BMS_1_device_cell_13_volt/config" in configs
        for n in (14, 15, 16):
            assert f"homeassistant/sensor/BMS_1_device_cell_{n}_volt/config" in removals
            assert f"homeassistant/sensor/BMS_1_device_cell_{n}_ohm/config" in removals
        assert not any("cell_13_" in t for t in removals)

    def test_no_cell_removals_at_max_cells(self) -> None:
        msgs = build_discovery_messages(settings=_settings(), bms_name="BMS_1", cell_count=16)
        # Per-cell entities only; writables like cell_soc100_voltage have removals too.
        assert not any(re.search(r"_device_cell_\d+_", t) for t in _removal_topics(msgs))

    def test_hidden_unverified_entities_are_removed(self) -> None:
        msgs = build_discovery_messages(settings=_settings(), bms_name="BMS_1", cell_count=16)
        removals = _removal_topics(msgs)
        assert "homeassistant/sensor/BMS_1_device_heating_current/config" in removals
        assert "homeassistant/binary_sensor/BMS_1_device_heating/config" in removals
        assert "homeassistant/switch/BMS_1_device_smart_sleep_switch/config" in removals
        assert "homeassistant/binary_sensor/BMS_1_device_smart_sleep_switch/config" in removals

    def test_render_removal_is_empty_payload(self) -> None:
        msg = DiscoveryMessage(topic="homeassistant/sensor/x/config", payload=None)
        assert render(msg) == ("homeassistant/sensor/x/config", b"")


class TestBuildDiscoveryMessages:
    def test_read_only_entities_always_published(self) -> None:
        s = _settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = _config_topics(msgs)
        # Topic shape: homeassistant/sensor/BMS_1_device_<obj>/config
        assert any(t == "homeassistant/sensor/BMS_1_device_total_voltage/config" for t in topics)
        assert any(t == "homeassistant/sensor/BMS_1_device_cell_1_volt/config" for t in topics)
        assert any(t == "homeassistant/sensor/BMS_1_device_bms_model/config" for t in topics)

    def test_writables_visible_as_status_when_toggles_off(self) -> None:
        s = _settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = _config_topics(msgs)
        # Both basic + safety numeric writables show up as plain sensor when
        # their tier is off — never silently hidden.
        assert any(
            "/sensor/BMS_1_device_max_charge_current/config" in t for t in topics
        )
        assert any(
            "/sensor/BMS_1_device_smart_sleep_voltage/config" in t for t in topics
        )

    def test_unverified_entities_hidden_by_default(self) -> None:
        s = _settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = _config_topics(msgs)
        # Packed-bit entities and the heating / charge_status fields are
        # unverified and so should not appear unless debug flag is on.
        assert not any("BMS_1_device_smart_sleep_switch" in t for t in topics)
        assert not any("BMS_1_device_heating" in t for t in topics)
        assert not any("BMS_1_device_charge_status" in t for t in topics)

    def test_unverified_entities_surface_with_debug_flag(self) -> None:
        s = _settings(debug_unverified_fields=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = _config_topics(msgs)
        assert any("BMS_1_device_smart_sleep_switch" in t for t in topics)
        assert any("BMS_1_device_heating" in t for t in topics)

    def test_basic_writables_when_basic_on(self) -> None:
        s = _settings(enable_basic_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = _config_topics(msgs)
        # Basic-tier numeric is now a `number`.
        assert any("/number/BMS_1_device_smart_sleep_voltage/config" in t for t in topics)
        # Safety-tier max_charge_current still appears, just as a sensor.
        assert any("/sensor/BMS_1_device_max_charge_current/config" in t for t in topics)
        assert not any("/number/BMS_1_device_max_charge_current/config" in t for t in topics)

    def test_safety_writables_when_safety_on(self) -> None:
        s = _settings(enable_safety_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = _config_topics(msgs)
        assert any("/number/BMS_1_device_max_charge_current/config" in t for t in topics)
        # Basic-tier numeric shows up as sensor.
        assert any("/sensor/BMS_1_device_smart_sleep_voltage/config" in t for t in topics)
        assert not any("/number/BMS_1_device_smart_sleep_voltage/config" in t for t in topics)

    def test_both_toggles_on_with_debug_publishes_packed_bit_as_switch(self) -> None:
        # Packed bits are unverified — they require debug_unverified_fields=True
        # to appear at all, and basic tier on to be writable.
        s = _settings(
            enable_basic_writes=True, enable_safety_writes=True,
            debug_unverified_fields=True,
        )
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = _config_topics(msgs)
        assert any("/switch/BMS_1_device_smart_sleep_switch/config" in t for t in topics)


class TestDiscoveryPayloads:
    def test_temperature_sensor(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "mos_temp")
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        assert msg.topic == "homeassistant/sensor/BMS_1_device_mos_temp/config"
        p = msg.payload
        assert p["device_class"] == "temperature"
        assert p["unit_of_measurement"] == "°C"
        assert p["state_topic"] == "BMS_1/Mos_temp"
        assert p["device"]["identifiers"] == ["BMS_1_device"]
        assert p["device"]["name"] == "BMS_1"
        assert p["unique_id"] == "BMS_1_device_mos_temp"
        assert p["suggested_display_precision"] == 1  # temps are 0.1 °C from BMS

    def test_voltage_sensor_full_mv_precision(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "total_voltage")
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        assert msg.payload["suggested_display_precision"] == 3

    def test_percent_sensor_zero_decimals(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "soc_percentage")
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        assert msg.payload["suggested_display_precision"] == 0

    def test_binary_sensor_omits_suggested_display_precision(self) -> None:
        from jkbms2mqtt.entities import LIVE_BINARY_SENSORS

        e = LIVE_BINARY_SENSORS[0]
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        assert "suggested_display_precision" not in msg.payload

    def test_writable_number_when_tier_enabled(self) -> None:
        w = next(x for x in WRITABLE_ENTITIES if x.object_id == "max_charge_current")
        msg = discovery_for_writable(
            w, "BMS_1", discovery_prefix="homeassistant", writable=True
        )
        p = msg.payload
        assert msg.topic.startswith("homeassistant/number/")
        assert p["command_topic"] == "BMS_1/control/max_charge_current/set"
        assert p["min"] == 0
        assert p["max"] == 600
        # max_charge_current now U32_MILLI (1 mA step) on this firmware.
        assert p["step"] == 0.001
        assert p["unit_of_measurement"] == "A"

    def test_writable_number_when_tier_disabled_becomes_sensor(self) -> None:
        w = next(x for x in WRITABLE_ENTITIES if x.object_id == "max_charge_current")
        msg = discovery_for_writable(
            w, "BMS_1", discovery_prefix="homeassistant", writable=False
        )
        assert msg.topic.startswith("homeassistant/sensor/")
        assert "command_topic" not in msg.payload
        assert msg.payload["state_topic"] == "BMS_1/control/max_charge_current"

    def test_writable_switch_when_tier_enabled(self) -> None:
        # No BOOL32 writable currently in the verified register table — build a
        # synthetic one to exercise the switch / binary-sensor discovery branch.
        from jkbms2mqtt.entities import Component, WritableEntity
        from jkbms2mqtt.protocol.jk_settings import Encoding, RegisterDef, WriteTier
        reg = RegisterDef(
            name="synthetic_switch", address=0x1090, encoding=Encoding.BOOL32,
            min_value=0, max_value=1, step=1, unit=None,
            tier=WriteTier.BASIC, description="test switch",
        )
        w = WritableEntity(
            object_id="synthetic_switch",
            topic_suffix="control/synthetic_switch",
            register=reg,
            component=Component.SWITCH,
            description="test switch",
        )
        msg = discovery_for_writable(
            w, "BMS_1", discovery_prefix="homeassistant", writable=True
        )
        assert msg.topic.startswith("homeassistant/switch/")
        assert msg.payload["payload_on"] == "ON"
        assert msg.payload["state_off"] == "OFF"

    def test_writable_switch_when_tier_disabled_becomes_binary_sensor(self) -> None:
        from jkbms2mqtt.entities import Component, WritableEntity
        from jkbms2mqtt.protocol.jk_settings import Encoding, RegisterDef, WriteTier
        reg = RegisterDef(
            name="synthetic_switch", address=0x1090, encoding=Encoding.BOOL32,
            min_value=0, max_value=1, step=1, unit=None,
            tier=WriteTier.BASIC, description="test switch",
        )
        w = WritableEntity(
            object_id="synthetic_switch",
            topic_suffix="control/synthetic_switch",
            register=reg,
            component=Component.SWITCH,
            description="test switch",
        )
        msg = discovery_for_writable(
            w, "BMS_1", discovery_prefix="homeassistant", writable=False
        )
        assert msg.topic.startswith("homeassistant/binary_sensor/")
        assert "command_topic" not in msg.payload

    def test_packed_bit_when_tier_enabled(self) -> None:
        bit = PACKED_BIT_ENTITIES[0]
        msg = discovery_for_packed_bit(
            bit, "BMS_1", discovery_prefix="homeassistant", writable=True
        )
        assert msg.topic.startswith("homeassistant/switch/BMS_1_device_")
        assert msg.payload["state_on"] == "ON"

    def test_packed_bit_when_tier_disabled(self) -> None:
        bit = PACKED_BIT_ENTITIES[0]
        msg = discovery_for_packed_bit(
            bit, "BMS_1", discovery_prefix="homeassistant", writable=False
        )
        assert msg.topic.startswith("homeassistant/binary_sensor/BMS_1_device_")
        assert "command_topic" not in msg.payload

    def test_entity_category_emitted_for_diagnostic_sensor(self) -> None:
        from jkbms2mqtt.entities import FIXED_SENSORS
        e = next(x for x in FIXED_SENSORS if x.object_id == "bms_model")
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        assert msg.payload["entity_category"] == "diagnostic"

    def test_entity_category_omitted_for_primary_sensor(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "total_voltage")
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        assert "entity_category" not in msg.payload

    def test_entity_category_emitted_for_writable_number(self) -> None:
        w = next(x for x in WRITABLE_ENTITIES if x.object_id == "max_charge_current")
        msg = discovery_for_writable(
            w, "BMS_1", discovery_prefix="homeassistant", writable=True
        )
        assert msg.payload["entity_category"] == "config"

    def test_entity_category_diagnostic_for_writable_when_downgraded_to_sensor(self) -> None:
        """When the tier is off the writable shows as a sensor; HA rejects
        entity_category=config there, so it becomes diagnostic."""
        w = next(x for x in WRITABLE_ENTITIES if x.object_id == "max_charge_current")
        msg = discovery_for_writable(
            w, "BMS_1", discovery_prefix="homeassistant", writable=False
        )
        assert msg.payload["entity_category"] == "diagnostic"

    def test_entity_category_diagnostic_for_packed_bit_when_downgraded(self) -> None:
        bit = PACKED_BIT_ENTITIES[0]
        msg = discovery_for_packed_bit(
            bit, "BMS_1", discovery_prefix="homeassistant", writable=False
        )
        assert msg.payload["entity_category"] == "diagnostic"

    def test_entity_category_emitted_for_packed_bit(self) -> None:
        bit = PACKED_BIT_ENTITIES[0]
        msg = discovery_for_packed_bit(
            bit, "BMS_1", discovery_prefix="homeassistant", writable=True
        )
        assert msg.payload["entity_category"] == "config"

    def test_entity_category_omitted_for_writable_without_category(self) -> None:
        """Synthetic WritableEntity with entity_category=None — covers the
        defensive branch in discovery_for_writable. No real writable currently
        uses entity_category=None, but the path exists in case a future
        register def needs to opt out (e.g. a primary on/off control)."""
        from jkbms2mqtt.entities import Component, WritableEntity
        from jkbms2mqtt.protocol.jk_settings import Encoding, RegisterDef, WriteTier
        reg = RegisterDef(
            name="primary_switch", address=0x1090, encoding=Encoding.BOOL32,
            min_value=0, max_value=1, step=1, unit=None,
            tier=WriteTier.BASIC, description="test primary switch",
        )
        w = WritableEntity(
            object_id="primary_switch",
            topic_suffix="control/primary_switch",
            register=reg,
            component=Component.SWITCH,
            description="test",
            entity_category=None,
        )
        msg = discovery_for_writable(
            w, "BMS_1", discovery_prefix="homeassistant", writable=True
        )
        assert "entity_category" not in msg.payload

    def test_entity_category_omitted_for_packed_bit_without_category(self) -> None:
        from jkbms2mqtt.entities import PackedBitEntity
        from jkbms2mqtt.protocol.jk_settings import PackedBitDef, WriteTier
        bit_def = PackedBitDef(
            name="primary_bit", register=0x1114, bit_mask=0x0001,
            tier=WriteTier.BASIC, description="test",
        )
        p = PackedBitEntity(
            object_id="primary_bit",
            topic_suffix="control/primary_bit",
            bit=bit_def,
            entity_category=None,
        )
        msg = discovery_for_packed_bit(
            p, "BMS_1", discovery_prefix="homeassistant", writable=True
        )
        assert "entity_category" not in msg.payload

    def test_render_returns_compact_json(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "soc_percentage")
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        topic, payload_bytes = render(msg)
        assert topic == msg.topic
        assert json.loads(payload_bytes) == msg.payload


class TestOrphanRemovals:
    """Clearing retained configs of packs the bridge no longer polls (issue #25)."""

    def _topic(self, component: str, bms: str, object_id: str, prefix: str = "homeassistant") -> str:
        return f"{prefix}/{component}/{bms}_device_{object_id}/config"

    def test_clears_a_pack_that_is_no_longer_configured(self) -> None:
        s = _settings(bms_ids=[1, 2])
        topics = [
            self._topic("sensor", "BMS_1", "total_voltage"),
            self._topic("sensor", "BMS_7", "total_voltage"),
            self._topic("number", "BMS_7", "max_charge_current"),
        ]
        removals = orphan_removals(topics, settings=s)
        assert [m.topic for m in removals] == topics[1:]
        assert all(m.payload is None for m in removals)

    def test_leaves_configured_packs_alone(self) -> None:
        s = _settings(bms_ids=[1, 2, 3])
        topics = [self._topic("sensor", f"BMS_{n}", "total_voltage") for n in (1, 2, 3)]
        assert orphan_removals(topics, settings=s) == []

    def test_ignores_another_integration(self) -> None:
        s = _settings(bms_ids=[1])
        topics = [
            "homeassistant/sensor/zigbee_thermostat/config",
            "homeassistant/light/kitchen_lamp/config",
            "zigbee2mqtt/bridge/config",
        ]
        assert orphan_removals(topics, settings=s) == []

    def test_ignores_another_bridge_under_a_different_prefix(self) -> None:
        """A second jkbms2mqtt with its own discovery prefix owns its topics."""
        s = _settings(bms_ids=[1])
        topics = [self._topic("sensor", "BMS_7", "total_voltage", prefix="ha-other")]
        assert orphan_removals(topics, settings=s) == []

    def test_respects_the_bms_name_prefix(self) -> None:
        s = _settings(bms_ids=[1], bms_name_prefix="PACK")
        topics = [
            self._topic("sensor", "PACK_9", "total_voltage"),
            self._topic("sensor", "BMS_9", "total_voltage"),  # another instance
        ]
        assert [m.topic for m in orphan_removals(topics, settings=s)] == [topics[0]]

    def test_ignores_an_unknown_component(self) -> None:
        s = _settings(bms_ids=[1])
        topics = [self._topic("climate", "BMS_7", "total_voltage")]
        assert orphan_removals(topics, settings=s) == []

    def test_ignores_a_malformed_device_id(self) -> None:
        s = _settings(bms_ids=[1])
        topics = [
            "homeassistant/sensor/BMS_x_device_total_voltage/config",  # non-numeric id
            "homeassistant/sensor/BMS_7_total_voltage/config",  # no _device_
            "homeassistant/sensor/BMS_7_device_/config",  # empty object_id
            "homeassistant/sensor/BMS_7_device_total_voltage/state",  # not a config topic
        ]
        assert orphan_removals(topics, settings=s) == []

    def test_empty_broker(self) -> None:
        assert orphan_removals([], settings=_settings(bms_ids=[1])) == []

    def test_removals_render_as_empty_payloads(self) -> None:
        s = _settings(bms_ids=[1])
        (removal,) = orphan_removals(
            [self._topic("binary_sensor", "BMS_4", "charging_switch")], settings=s
        )
        assert render(removal) == (
            "homeassistant/binary_sensor/BMS_4_device_charging_switch/config",
            b"",
        )


# -- State messages -------------------------------------------------------------------


class TestFreshness:
    def test_every_entity_follows_bridge_availability_except_last_seen(self) -> None:
        s = _settings(
            enable_basic_writes=True, enable_safety_writes=True,
            debug_unverified_fields=True,
        )
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        for m in msgs:
            if m.payload is None:
                continue
            if m.payload["unique_id"] == "BMS_1_device_last_seen":
                assert "availability_topic" not in m.payload
            else:
                assert m.payload["availability_topic"] == BRIDGE_AVAILABILITY_TOPIC, m.topic

    def test_last_seen_discovery_is_timestamp_sensor(self) -> None:
        msgs = build_discovery_messages(settings=_settings(), bms_name="BMS_1", cell_count=16)
        m = next(
            x for x in msgs
            if x.payload is not None and x.payload["unique_id"] == "BMS_1_device_last_seen"
        )
        assert m.topic == "homeassistant/sensor/BMS_1_device_last_seen/config"
        assert m.payload["device_class"] == "timestamp"
        assert m.payload["state_topic"] == "BMS_1/Last_seen"
        assert m.payload["name"] == "Last seen"
        assert "entity_category" not in m.payload
        assert "suggested_display_precision" not in m.payload

    def test_no_entity_id_is_suggested(self) -> None:
        """HA derives the entity id from device name + entity name; the bridge
        must not override it (``object_id`` was removed in HA 2026.4, and
        ``default_entity_id`` is an override we deliberately don't send)."""
        s = _settings(
            enable_basic_writes=True, enable_safety_writes=True, debug_unverified_fields=True
        )
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        for m in msgs:
            if m.payload is None:
                continue
            assert "object_id" not in m.payload, m.topic
            assert "default_entity_id" not in m.payload, m.topic

    def test_unique_id_still_identifies_the_entity(self) -> None:
        """The unique_id keeps its shape: it is how HA recognises an entity
        across restarts and renames, and how the rename script matches them."""
        e = next(x for x in LIVE_SENSORS if x.object_id == "total_voltage")
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        assert msg.payload["unique_id"] == "BMS_1_device_total_voltage"
        assert msg.payload["name"] == "Total voltage"

    def test_state_message_last_seen_iso_with_timezone(self) -> None:
        when = datetime(2026, 9, 15, 10, 21, 7, 123456, tzinfo=UTC)
        assert state_message_last_seen("BMS_1", when) == (
            "BMS_1/Last_seen", "2026-09-15T10:21:07+00:00",
        )

    def test_state_message_last_seen_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            state_message_last_seen("BMS_1", datetime(2026, 9, 15, 10, 21, 7))


class TestStateMessagesFromLive:
    def test_emits_expected_topics(self) -> None:
        msgs = state_messages_from_live(_sample_realtime(), "BMS_1")
        topics = {t for t, _ in msgs}
        assert "BMS_1/Total_Voltage_V" in topics
        assert "BMS_1/SOC_percentage" in topics
        assert "BMS_1/Cell_1_volt" in topics
        assert "BMS_1/Cell_16_volt" in topics
        assert "BMS_1/Mos_temp" in topics

    def test_binary_sensors_render_on_off(self) -> None:
        msgs = dict(state_messages_from_live(_sample_realtime(), "BMS_1"))
        assert msgs["BMS_1/Switch_Charge"] == "ON"
        assert msgs["BMS_1/Switch_Balance"] == "OFF"

    def test_float_values_three_decimals(self) -> None:
        msgs = dict(state_messages_from_live(_sample_realtime(), "BMS_1"))
        assert msgs["BMS_1/Total_Voltage_V"] == "53.000"

    def test_temperatures_have_one_decimal(self) -> None:
        msgs = dict(state_messages_from_live(_sample_realtime(), "BMS_1"))
        # mos_temp_c=25.0 → "25.0"  (NOT "25.000" — that's 2 fake zeros)
        assert msgs["BMS_1/Mos_temp"] == "25.0"

    def test_percentages_render_as_integer_strings(self) -> None:
        msgs = dict(state_messages_from_live(_sample_realtime(), "BMS_1"))
        # SoC is u8 from the BMS — no decimals are meaningful.
        assert msgs["BMS_1/SOC_percentage"] == "75"

    def test_cell_voltages_have_three_decimals(self) -> None:
        msgs = dict(state_messages_from_live(_sample_realtime(cell_count=8), "BMS_1"))
        # Cell 1 = 3.300 V from the fixture
        assert msgs["BMS_1/Cell_1_volt"] == "3.300"

    def test_current_three_decimals(self) -> None:
        msgs = dict(state_messages_from_live(_sample_realtime(), "BMS_1"))
        assert msgs["BMS_1/Total_Current_A"] == "10.000"

    def test_per_cell_count_matches_cells(self) -> None:
        msgs = state_messages_from_live(_sample_realtime(cell_count=8), "BMS_1")
        cell_topics = {t for t, _ in msgs if t.startswith("BMS_1/Cell_")}
        # 8 voltage topics + 8 resistance topics
        assert len(cell_topics) == 16
        volt_topics = {t for t in cell_topics if t.endswith("_volt")}
        ohm_topics = {t for t in cell_topics if t.endswith("_ohm")}
        assert len(volt_topics) == 8
        assert len(ohm_topics) == 8


class TestStateMessagesFromStatic:
    def test_emits_static_info(self) -> None:
        msgs = dict(state_messages_from_static(_sample_static(), "BMS_1"))
        assert msgs["BMS_1/bms"] == "JK-PB2A16S15P"
        assert msgs["BMS_1/fw"] == "HW10A20H"
        assert msgs["BMS_1/sw"] == "SW1209HE"
        assert msgs["BMS_1/serialnb"] == "JK202401012345"


class TestStateMessagesFromSettings:
    def test_emits_numeric_topics(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import (
            BASIC_REGISTERS,
            PACKED_BITS,
            SAFETY_REGISTERS,
        )

        max_chg = next(r for r in SAFETY_REGISTERS if r.name == "max_charge_current")
        sleep_v = next(r for r in BASIC_REGISTERS if r.name == "smart_sleep_voltage")
        sleep_bit = next(b for b in PACKED_BITS if b.name == "smart_sleep_switch")

        msgs = dict(
            state_messages_from_settings(
                register_values={max_chg: 40.0, sleep_v: 3.500},
                packed_values={sleep_bit: True},
                bms_name="BMS_1",
                debug_unverified=True,  # packed bits are unverified by default
            )
        )
        # max_charge_current uses U32_MILLI now → 3 decimals.
        assert msgs["BMS_1/control/max_charge_current"] == "40.000"
        assert msgs["BMS_1/control/smart_sleep_voltage"] == "3.500"
        assert msgs["BMS_1/control/smart_sleep_switch"] == "ON"

    def test_packed_bits_hidden_without_debug_flag(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import PACKED_BITS

        sleep_bit = next(b for b in PACKED_BITS if b.name == "smart_sleep_switch")
        msgs = dict(
            state_messages_from_settings(
                register_values={},
                packed_values={sleep_bit: True},
                bms_name="BMS_1",
            )
        )
        assert msgs == {}

    def test_skips_registers_without_value(self) -> None:
        msgs = state_messages_from_settings(
            register_values={}, packed_values={}, bms_name="BMS_1"
        )
        assert msgs == []


class TestFormat:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, "ON"),
            (False, "OFF"),
            (3.14159, "3.142"),       # default 3 decimals when not specified
            (42, "42"),
            ("hello", "hello"),
        ],
    )
    def test_default_examples(self, value: object, expected: str) -> None:
        assert _format(value) == expected

    @pytest.mark.parametrize(
        ("value", "decimals", "expected"),
        [
            (3.301, 3, "3.301"),
            (24.7, 1, "24.7"),
            (3.7, 0, "4"),            # rounds; integer-formatted (no decimal point)
            (3.4, 0, "3"),
            (3.300, None, "3.300"),   # None preserves the old 3-decimal default
        ],
    )
    def test_with_decimals(self, value: float, decimals: int | None, expected: str) -> None:
        assert _format(value, decimals) == expected
