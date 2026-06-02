"""HA Discovery + state-message publisher tests."""

from __future__ import annotations

import json

import pytest

from jkbms2mqtt.config import Settings, Transport
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
    state_messages_from_live,
    state_messages_from_static,
)
from jkbms2mqtt.protocol.jk_modbus import JkRealtime, JkStaticInfo


def _settings(**overrides) -> Settings:
    base = {"transport": Transport.TCP_GATEWAY, "gateway_host": "x.x.x.x", "gateway_port": 502}
    base.update(overrides)
    return Settings(**base)


def _sample_realtime(*, cell_count: int = 16) -> JkRealtime:
    cells = tuple(3.300 + i / 1000 for i in range(cell_count))
    return JkRealtime(
        cell_voltages_v=cells,
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
        runtime_s=86400,
        charge_enabled=True,
        discharge_enabled=True,
        alarm_bits=0,
        alarms=(),
    )


def _sample_static() -> JkStaticInfo:
    return JkStaticInfo(
        model="JK-PB2A16S15P",
        hw_version="HW10A20H",
        sw_version="SW1209HE",
        serial_number="JK202401012345",
    )


# -- Discovery messages ---------------------------------------------------------------


class TestBuildDiscoveryMessages:
    def test_read_only_entities_always_published(self) -> None:
        s = _settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        # Topic shape: homeassistant/sensor/BMS_1_device_<obj>/config
        assert any(t == "homeassistant/sensor/BMS_1_device_total_voltage/config" for t in topics)
        assert any(t == "homeassistant/sensor/BMS_1_device_cell_1_volt/config" for t in topics)
        assert any(t == "homeassistant/sensor/BMS_1_device_bms_model/config" for t in topics)

    def test_writables_hidden_when_toggles_off(self) -> None:
        s = _settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        # No writable entities should appear
        assert not any("/switch/" in t for t in topics if "Switch_" not in t)
        assert not any("/number/" in t for t in topics)

    def test_basic_writables_when_basic_on(self) -> None:
        s = _settings(enable_basic_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        assert any("/switch/BMS_1_device_charging_switch/config" in t for t in topics)
        assert not any("/number/BMS_1_device_max_charge_current/config" in t for t in topics)

    def test_safety_writables_when_safety_on(self) -> None:
        s = _settings(enable_safety_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        assert any("/number/BMS_1_device_max_charge_current/config" in t for t in topics)
        assert not any("/switch/BMS_1_device_charging_switch/config" in t for t in topics)

    def test_both_toggles_on_publishes_packed_bit_too(self) -> None:
        s = _settings(enable_basic_writes=True, enable_safety_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
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

    def test_writable_number(self) -> None:
        w = next(x for x in WRITABLE_ENTITIES if x.object_id == "max_charge_current")
        msg = discovery_for_writable(w, "BMS_1", discovery_prefix="homeassistant")
        p = msg.payload
        assert p["command_topic"] == "BMS_1/control/max_charge_current/set"
        assert p["min"] == 0
        assert p["max"] == 600
        assert p["step"] == 0.1
        assert p["unit_of_measurement"] == "A"

    def test_writable_switch(self) -> None:
        w = next(x for x in WRITABLE_ENTITIES if x.object_id == "charging_switch")
        msg = discovery_for_writable(w, "BMS_1", discovery_prefix="homeassistant")
        assert msg.payload["payload_on"] == "ON"
        assert msg.payload["state_off"] == "OFF"

    def test_packed_bit(self) -> None:
        bit = PACKED_BIT_ENTITIES[0]
        msg = discovery_for_packed_bit(bit, "BMS_1", discovery_prefix="homeassistant")
        assert msg.topic.startswith("homeassistant/switch/BMS_1_device_")
        assert msg.payload["state_on"] == "ON"

    def test_render_returns_compact_json(self) -> None:
        e = next(x for x in LIVE_SENSORS if x.object_id == "soc_percentage")
        msg = discovery_for_read_only(e, "BMS_1", discovery_prefix="homeassistant")
        topic, payload_bytes = render(msg)
        assert topic == msg.topic
        assert json.loads(payload_bytes) == msg.payload


# -- State messages -------------------------------------------------------------------


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

    def test_per_cell_count_matches_cells(self) -> None:
        msgs = state_messages_from_live(_sample_realtime(cell_count=8), "BMS_1")
        cell_topics = {t for t, _ in msgs if t.startswith("BMS_1/Cell_")}
        assert len(cell_topics) == 8


class TestStateMessagesFromStatic:
    def test_emits_static_info(self) -> None:
        msgs = dict(state_messages_from_static(_sample_static(), "BMS_1"))
        assert msgs["BMS_1/bms"] == "JK-PB2A16S15P"
        assert msgs["BMS_1/fw"] == "HW10A20H"
        assert msgs["BMS_1/sw"] == "SW1209HE"
        assert msgs["BMS_1/serialnb"] == "JK202401012345"


class TestFormat:
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
    def test_examples(self, value: object, expected: str) -> None:
        assert _format(value) == expected
