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
        charge_status_id=1,
        charge_status="bulk",
        charge_status_time_s=600,
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


class TestBuildDiscoveryMessages:
    def test_read_only_entities_always_published(self) -> None:
        s = _settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        # Topic shape: homeassistant/sensor/BMS_1_device_<obj>/config
        assert any(t == "homeassistant/sensor/BMS_1_device_total_voltage/config" for t in topics)
        assert any(t == "homeassistant/sensor/BMS_1_device_cell_1_volt/config" for t in topics)
        assert any(t == "homeassistant/sensor/BMS_1_device_bms_model/config" for t in topics)

    def test_writables_visible_as_status_when_toggles_off(self) -> None:
        s = _settings()
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        # Writables show up as sensor / binary_sensor — never silently hidden.
        assert any(
            "/sensor/BMS_1_device_max_charge_current/config" in t for t in topics
        )
        assert any(
            "/binary_sensor/BMS_1_device_charging_switch/config" in t for t in topics
        )
        assert any(
            "/binary_sensor/BMS_1_device_smart_sleep_switch/config" in t for t in topics
        )

    def test_basic_writables_when_basic_on(self) -> None:
        s = _settings(enable_basic_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        assert any("/switch/BMS_1_device_charging_switch/config" in t for t in topics)
        # Safety-tier max_charge_current still appears, just as a sensor.
        assert any("/sensor/BMS_1_device_max_charge_current/config" in t for t in topics)
        assert not any("/number/BMS_1_device_max_charge_current/config" in t for t in topics)

    def test_safety_writables_when_safety_on(self) -> None:
        s = _settings(enable_safety_writes=True)
        msgs = build_discovery_messages(settings=s, bms_name="BMS_1", cell_count=16)
        topics = [m.topic for m in msgs]
        assert any("/number/BMS_1_device_max_charge_current/config" in t for t in topics)
        # Basic-tier charging_switch shows up as binary_sensor.
        assert any("/binary_sensor/BMS_1_device_charging_switch/config" in t for t in topics)
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
        assert p["step"] == 0.1
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
        w = next(x for x in WRITABLE_ENTITIES if x.object_id == "charging_switch")
        msg = discovery_for_writable(
            w, "BMS_1", discovery_prefix="homeassistant", writable=True
        )
        assert msg.topic.startswith("homeassistant/switch/")
        assert msg.payload["payload_on"] == "ON"
        assert msg.payload["state_off"] == "OFF"

    def test_writable_switch_when_tier_disabled_becomes_binary_sensor(self) -> None:
        w = next(x for x in WRITABLE_ENTITIES if x.object_id == "charging_switch")
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
    def test_emits_numeric_and_boolean_topics(self) -> None:
        from jkbms2mqtt.protocol.jk_settings import (
            BASIC_REGISTERS,
            PACKED_BITS,
            SAFETY_REGISTERS,
        )

        max_chg = next(r for r in SAFETY_REGISTERS if r.name == "max_charge_current")
        charging = next(r for r in BASIC_REGISTERS if r.name == "charging_switch")
        sleep_bit = next(b for b in PACKED_BITS if b.name == "smart_sleep_switch")

        msgs = dict(
            state_messages_from_settings(
                register_values={max_chg: 80.0, charging: True},
                packed_values={sleep_bit: True},
                bms_name="BMS_1",
            )
        )
        assert msgs["BMS_1/control/max_charge_current"] == "80.0"
        assert msgs["BMS_1/control/charging_switch"] == "ON"
        assert msgs["BMS_1/control/smart_sleep_switch"] == "ON"

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
