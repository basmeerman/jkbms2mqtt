"""Config validation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jkbms2mqtt.config import (
    LogLevel,
    Settings,
    Topology,
    Transport,
    _coerce_env_value,
    load_settings,
)


class TestTransportCombos:
    def test_default_construct(self) -> None:
        s = Settings()
        assert s.transport is Transport.TCP_GATEWAY
        assert s.bms_ids == [1]
        assert s.topology is Topology.MASTER_POLL
        assert s.log_level is LogLevel.INFO
        assert s.mqtt_host == "core-mosquitto.local.hass.io"

    def test_tcp_gateway_empty_host_rejected(self) -> None:
        with pytest.raises(ValueError, match="gateway_host"):
            Settings(transport=Transport.TCP_GATEWAY, gateway_host="")

    def test_usb_serial_empty_path_rejected(self) -> None:
        with pytest.raises(ValueError, match="jkbms_path"):
            Settings(transport=Transport.USB_SERIAL, jkbms_path="")

    def test_usb_serial_with_path_ok(self) -> None:
        s = Settings(transport=Transport.USB_SERIAL, jkbms_path="/dev/ttyUSB0")
        assert s.transport is Transport.USB_SERIAL


class TestBmsIds:
    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            Settings(bms_ids=[])

    def test_duplicates_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicates"):
            Settings(bms_ids=[1, 2, 2])

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValueError):
            Settings(bms_ids=[0])

    def test_above_15_rejected(self) -> None:
        with pytest.raises(ValueError):
            Settings(bms_ids=[1, 16])

    def test_six_bms_accepted(self) -> None:
        s = Settings(bms_ids=[1, 2, 3, 4, 5, 6])
        assert s.bms_ids == [1, 2, 3, 4, 5, 6]

    def test_non_contiguous_accepted(self) -> None:
        s = Settings(bms_ids=[2, 5, 7])
        assert s.bms_ids == [2, 5, 7]

    def test_string_parsed_as_comma_separated(self) -> None:
        s = Settings(bms_ids="1,2,3,4,5,6")  # type: ignore[arg-type]
        assert s.bms_ids == [1, 2, 3, 4, 5, 6]

    def test_string_with_whitespace_parsed(self) -> None:
        s = Settings(bms_ids=" 2 , 5 , 7 ")  # type: ignore[arg-type]
        assert s.bms_ids == [2, 5, 7]

    def test_string_single_value_parsed(self) -> None:
        s = Settings(bms_ids="3")  # type: ignore[arg-type]
        assert s.bms_ids == [3]

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            Settings(bms_ids="")  # type: ignore[arg-type]

    def test_string_with_invalid_int_rejected(self) -> None:
        with pytest.raises(ValueError):
            Settings(bms_ids="1,abc,3")  # type: ignore[arg-type]

    def test_string_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            Settings(bms_ids="1,2,99")  # type: ignore[arg-type]


class TestNumericRanges:
    def test_poll_interval_lower_bound(self) -> None:
        with pytest.raises(ValueError):
            Settings(poll_interval_s=0.5)

    def test_poll_interval_upper_bound(self) -> None:
        with pytest.raises(ValueError):
            Settings(poll_interval_s=61)

    def test_mqtt_port_range(self) -> None:
        with pytest.raises(ValueError):
            Settings(mqtt_port=0)
        with pytest.raises(ValueError):
            Settings(mqtt_port=70000)

    def test_gateway_port_range(self) -> None:
        with pytest.raises(ValueError):
            Settings(gateway_port=0)


class TestPrefixes:
    def test_empty_bms_name_prefix_rejected(self) -> None:
        with pytest.raises(ValueError, match="bms_name_prefix"):
            Settings(bms_name_prefix="")

    def test_custom_prefix_accepted(self) -> None:
        s = Settings(bms_name_prefix="Pack")
        assert s.bms_name_prefix == "Pack"


class TestLoadSettings:
    def test_from_options_json(self, tmp_path: Path) -> None:
        opts = {
            "transport": "tcp_gateway",
            "gateway_host": "10.0.0.10",
            "gateway_port": 502,
            "bms_ids": [1, 2, 3, 4, 5, 6],
            "poll_interval_s": 5,
        }
        p = tmp_path / "options.json"
        p.write_text(json.dumps(opts))
        s = load_settings(options_path=p, env={})
        assert s.gateway_host == "10.0.0.10"
        assert s.bms_ids == [1, 2, 3, 4, 5, 6]

    def test_from_env(self, tmp_path: Path) -> None:
        env = {
            "JKBMS2MQTT_TRANSPORT": "tcp_gateway",
            "JKBMS2MQTT_GATEWAY_HOST": "1.2.3.4",
            "JKBMS2MQTT_GATEWAY_PORT": "8000",
            "JKBMS2MQTT_BMS_IDS": "1,2,3,4,5,6",
            "JKBMS2MQTT_ENABLE_BASIC_WRITES": "true",
            "JKBMS2MQTT_POLL_INTERVAL_S": "2.5",
            "JKBMS2MQTT_LOG_LEVEL": "debug",
            "JKBMS2MQTT_RECORDING_ENABLED": "true",
        }
        s = load_settings(options_path=tmp_path / "absent.json", env=env)
        assert s.gateway_host == "1.2.3.4"
        assert s.gateway_port == 8000
        assert s.bms_ids == [1, 2, 3, 4, 5, 6]
        assert s.enable_basic_writes is True
        assert s.poll_interval_s == 2.5
        assert s.log_level is LogLevel.DEBUG
        assert s.recording_enabled is True

    def test_yaml_overrides_options(self, tmp_path: Path) -> None:
        opts = {"transport": "tcp_gateway", "gateway_host": "1.1.1.1"}
        opts_path = tmp_path / "options.json"
        opts_path.write_text(json.dumps(opts))
        yml = tmp_path / "extra.yaml"
        yml.write_text("gateway_host: 2.2.2.2\n")
        env = {"JKBMS2MQTT_CONFIG_YAML": str(yml)}
        s = load_settings(options_path=opts_path, env=env)
        assert s.gateway_host == "2.2.2.2"

    def test_env_overrides_yaml(self, tmp_path: Path) -> None:
        yml = tmp_path / "extra.yaml"
        yml.write_text("transport: tcp_gateway\ngateway_host: 2.2.2.2\n")
        env = {
            "JKBMS2MQTT_CONFIG_YAML": str(yml),
            "JKBMS2MQTT_GATEWAY_HOST": "3.3.3.3",
        }
        s = load_settings(options_path=tmp_path / "absent.json", env=env)
        assert s.gateway_host == "3.3.3.3"

    def test_options_must_be_object(self, tmp_path: Path) -> None:
        p = tmp_path / "options.json"
        p.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="JSON object"):
            load_settings(options_path=p, env={})

    def test_yaml_must_be_mapping(self, tmp_path: Path) -> None:
        yml = tmp_path / "x.yaml"
        yml.write_text("- 1\n- 2\n")
        env = {
            "JKBMS2MQTT_CONFIG_YAML": str(yml),
            "JKBMS2MQTT_TRANSPORT": "tcp_gateway",
            "JKBMS2MQTT_GATEWAY_HOST": "1.1.1.1",
        }
        with pytest.raises(ValueError, match="YAML mapping"):
            load_settings(options_path=tmp_path / "absent.json", env=env)

    def test_empty_yaml_treated_as_no_overrides(self, tmp_path: Path) -> None:
        yml = tmp_path / "empty.yaml"
        yml.write_text("")
        env = {
            "JKBMS2MQTT_CONFIG_YAML": str(yml),
            "JKBMS2MQTT_TRANSPORT": "tcp_gateway",
            "JKBMS2MQTT_GATEWAY_HOST": "1.1.1.1",
        }
        s = load_settings(options_path=tmp_path / "absent.json", env=env)
        assert s.gateway_host == "1.1.1.1"

    def test_default_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Exercises ``options_path is None`` and ``env is None`` fallbacks."""
        from jkbms2mqtt import config as cm

        monkeypatch.setattr(cm, "HA_OPTIONS_PATH", tmp_path / "absent.json")
        monkeypatch.setenv("JKBMS2MQTT_TRANSPORT", "tcp_gateway")
        monkeypatch.setenv("JKBMS2MQTT_GATEWAY_HOST", "via-env")
        s = load_settings()
        assert s.gateway_host == "via-env"

    def test_non_prefixed_env_var_ignored(self, tmp_path: Path) -> None:
        env = {
            "PATH": "/usr/bin",
            "JKBMS2MQTT_TRANSPORT": "tcp_gateway",
            "JKBMS2MQTT_GATEWAY_HOST": "x.x.x.x",
        }
        s = load_settings(options_path=tmp_path / "absent.json", env=env)
        assert s.gateway_host == "x.x.x.x"

    def test_config_yaml_env_var_does_not_become_field(self, tmp_path: Path) -> None:
        yml = tmp_path / "extra.yaml"
        yml.write_text("gateway_host: 9.9.9.9\n")
        env = {"JKBMS2MQTT_CONFIG_YAML": str(yml)}
        s = load_settings(options_path=tmp_path / "absent.json", env=env)
        assert s.gateway_host == "9.9.9.9"


class TestCoerceEnvValue:
    def test_bool(self) -> None:
        assert _coerce_env_value("true") is True
        assert _coerce_env_value("FALSE") is False

    def test_int(self) -> None:
        assert _coerce_env_value("502") == 502
        assert _coerce_env_value("-1") == -1

    def test_float(self) -> None:
        assert _coerce_env_value("2.5") == 2.5

    def test_string(self) -> None:
        assert _coerce_env_value("hello") == "hello"


class TestExtraFieldsRejected:
    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValueError):
            Settings(unknown_field=42)  # type: ignore[call-arg]
