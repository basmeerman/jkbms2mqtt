"""MQTT discovery + state-message tests."""

from __future__ import annotations

import json

import pytest

from jkbms2mqtt.config import Settings
from jkbms2mqtt.entities import (
    LIVE_SENSORS,
    PACKED_BIT_ENTITIES,
    WRITABLE_ENTITIES,
)
from jkbms2mqtt.mqtt import (
    _format,
    build_discovery_messages,
    discovery_for_packed_bit,
    discovery_for_read_only,
    discovery_for_writable,
    render,
    state_messages_from_fixed,
    state_messages_from_live,
    state_messages_from_setup,
)
from jkbms2mqtt.protocol.capabilities import Topology, Transport


def _basic_settings(**overrides) -> Settings:
    base = {
        "transport": Transport.TCP_GATEWAY,
        "gateway_host": "x.x.x.x",
        "gateway_port": 502,
        "topology": Topology.MASTER_POLL,
    }
    base.update(overrides)
    return Settings(**base)


class TestBuildDiscoveryMessages:
    def test_read_only_entities_always_present(self) -> None:
        s = _basic_settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=8)
        topics = [m.topic for m in msgs]
        # Spot-checks: a known live sensor, a known cell sensor, a known fixed sensor.
        assert any("homeassistant/sensor/jkbms_bms_1/total_voltage/config" in t for t in topics)
        assert any("homeassistant/sensor/jkbms_bms_1/cell_1_volt/config" in t for t in topics)
        assert any("homeassistant/sensor/jkbms_bms_1/bms_model/config" in t for t in topics)

    def test_no_writes_in_broadcast_mode(self) -> None:
        s = _basic_settings(topology=Topology.BROADCAST, enable_basic_writes=True, enable_safety_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        # No writable discovery payload should appear.
        for m in msgs:
            assert "/switch/" not in m.topic or "Switch_" in m.topic  # binary switch sensors are ok
            assert "/number/" not in m.topic

    def test_writes_advertised_only_when_tier_enabled(self) -> None:
        # Only basic toggle on
        s = _basic_settings(enable_basic_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        # Basic-tier entity present
        assert any("/switch/jkbms_bms_1/charging_switch/config" in t for t in topics)
        # Safety-tier entity absent
        assert not any("/number/jkbms_bms_1/max_charge_current/config" in t for t in topics)

    def test_safety_writes_advertised_when_safety_toggle_on(self) -> None:
        s = _basic_settings(enable_safety_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        assert any("/number/jkbms_bms_1/max_charge_current/config" in t for t in topics)
        # But basic switches still absent (basic toggle off).
        assert not any("/switch/jkbms_bms_1/charging_switch/config" in t for t in topics)

    def test_both_tiers_enabled_publishes_all_writables(self) -> None:
        s = _basic_settings(enable_basic_writes=True, enable_safety_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        assert any("/switch/jkbms_bms_1/balance_switch/config" in t for t in topics)
        assert any("/number/jkbms_bms_1/max_charge_current/config" in t for t in topics)
        # Packed-bit switch
        assert any("/switch/jkbms_bms_1/smart_sleep_switch/config" in t for t in topics)

    def test_custom_slug_used_in_topic(self) -> None:
        s = _basic_settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_X", cell_count=8, slug="custom")
        assert any("homeassistant/sensor/custom/" in m.topic for m in msgs)


class TestDiscoveryPayloads:
    def test_temperature_sensor_payload(self) -> None:
        # Pick a known temperature sensor
        entity = next(e for e in LIVE_SENSORS if e.object_id == "mos_temp")
        msg = discovery_for_read_only(entity, "BMS_1", slug="jkbms_bms_1", discovery_prefix="homeassistant")
        assert msg.topic == "homeassistant/sensor/jkbms_bms_1/mos_temp/config"
        payload = msg.payload
        assert payload["device_class"] == "temperature"
        assert payload["unit_of_measurement"] == "°C"
        assert payload["state_topic"] == "BMS_1/Mos_temp"
        assert payload["device"]["identifiers"] == ["jkbms_bms_1"]

    def test_writable_number_payload(self) -> None:
        w = next(e for e in WRITABLE_ENTITIES if e.object_id == "max_charge_current")
        msg = discovery_for_writable(w, "BMS_1", slug="jkbms_bms_1", discovery_prefix="homeassistant")
        assert msg.topic == "homeassistant/number/jkbms_bms_1/max_charge_current/config"
        payload = msg.payload
        assert payload["command_topic"] == "BMS_1/control/max_charge_current/set"
        assert payload["min"] == 0
        assert payload["max"] == 600
        assert payload["step"] == 0.1
        assert payload["unit_of_measurement"] == "A"

    def test_writable_switch_payload(self) -> None:
        w = next(e for e in WRITABLE_ENTITIES if e.object_id == "charging_switch")
        msg = discovery_for_writable(w, "BMS_1", slug="jkbms_bms_1", discovery_prefix="homeassistant")
        payload = msg.payload
        assert payload["payload_on"] == "ON"
        assert payload["state_off"] == "OFF"

    def test_packed_bit_payload(self) -> None:
        bit = PACKED_BIT_ENTITIES[0]
        msg = discovery_for_packed_bit(bit, "BMS_1", slug="jkbms_bms_1", discovery_prefix="homeassistant")
        assert msg.topic.startswith("homeassistant/switch/jkbms_bms_1/")
        assert msg.payload["state_on"] == "ON"

    def test_render_serialises_compact_json(self) -> None:
        entity = next(e for e in LIVE_SENSORS if e.object_id == "soc_percentage")
        msg = discovery_for_read_only(entity, "BMS_1", slug="jkbms_bms_1", discovery_prefix="homeassistant")
        topic, payload_bytes = render(msg)
        # Round-trip: decoded JSON equals the original dict
        assert json.loads(payload_bytes) == msg.payload
        # Topic unchanged
        assert topic == msg.topic

    def test_binary_sensor_payload_on_off(self) -> None:
        from jkbms2mqtt.entities import LIVE_BINARY_SENSORS

        bs = LIVE_BINARY_SENSORS[0]
        msg = discovery_for_read_only(bs, "BMS_1", slug="jkbms_bms_1", discovery_prefix="homeassistant")
        assert msg.payload["payload_on"] == "ON"
        assert msg.payload["payload_off"] == "OFF"


class TestStateMessagesFromLive:
    def test_emits_every_known_topic(self, live_frame) -> None:
        from jkbms2mqtt.protocol.decoder import decode_live

        data = decode_live(live_frame(), cell_count=16)
        msgs = state_messages_from_live(data, "BMS_1")
        topics = {t for t, _ in msgs}
        assert "BMS_1/Total_Voltage_V" in topics
        assert "BMS_1/Cell_1_volt" in topics
        assert "BMS_1/Cell_16_ohm" in topics
        assert "BMS_1/Switch_Charge" in topics

    def test_binary_sensor_values_render_on_off(self, live_frame) -> None:
        from jkbms2mqtt.protocol.decoder import decode_live

        data = decode_live(live_frame(), cell_count=16)
        msgs = dict(state_messages_from_live(data, "BMS_1"))
        assert msgs["BMS_1/Switch_Charge"] == "ON"
        assert msgs["BMS_1/Switch_Balance"] == "OFF"

    def test_float_values_have_three_decimals(self, live_frame) -> None:
        from jkbms2mqtt.protocol.decoder import decode_live

        data = decode_live(live_frame(), cell_count=16)
        msgs = dict(state_messages_from_live(data, "BMS_1"))
        # Total_Voltage 53.00 → "53.000"
        assert msgs["BMS_1/Total_Voltage_V"] == "53.000"


class TestStateMessagesFromSetup:
    def test_writable_state_topics_reflect_current_values(self, setup_frame) -> None:
        from jkbms2mqtt.protocol.decoder import decode_setup

        data = decode_setup(setup_frame())
        msgs = dict(state_messages_from_setup(data, "BMS_1"))
        # A few spot checks: bool switch, voltage, current
        assert msgs["BMS_1/control/charging_switch"] == "ON"
        assert msgs["BMS_1/control/cell_voltage_overvoltage_protection"] == "3.650"
        assert msgs["BMS_1/control/max_charge_current"] == "50.000"
        assert msgs["BMS_1/control/smart_sleep_switch"] == "ON"


class TestStateMessagesFromFixed:
    def test_topics_match_expected(self, fixed_frame) -> None:
        from jkbms2mqtt.protocol.decoder import decode_fixed

        data = decode_fixed(fixed_frame())
        msgs = dict(state_messages_from_fixed(data, "BMS_1"))
        assert msgs["BMS_1/bms"] == "JK_PB2A16S15P"
        assert msgs["BMS_1/fw"] == "15A6.0"
        assert msgs["BMS_1/manufacturing_date"] == "240315"


class TestFormatHelper:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, "ON"),
            (False, "OFF"),
            (3.14159, "3.142"),
            (42, "42"),
            ("hello", "hello"),
        ],
    )
    def test_format_cases(self, value: object, expected: str) -> None:
        assert _format(value) == expected
