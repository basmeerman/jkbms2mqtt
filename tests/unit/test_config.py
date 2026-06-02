"""Config validation tests — covers the cross-field policy and ensures baud
rate is not user-tunable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jkbms2mqtt.config import Settings, load_settings
from jkbms2mqtt.protocol.capabilities import Topology, Transport


class TestTransportCombos:
    def test_tcp_gateway_with_host_and_port_ok(self) -> None:
        s = Settings(
            transport=Transport.TCP_GATEWAY,
            gateway_host="192.168.1.10",
            gateway_port=502,
        )
        assert s.transport is Transport.TCP_GATEWAY

    def test_tcp_gateway_missing_host_fails(self) -> None:
        with pytest.raises(ValueError, match="gateway_host and gateway_port"):
            Settings(transport=Transport.TCP_GATEWAY, gateway_port=502)

    def test_tcp_gateway_with_serial_path_fails_issue_4(self) -> None:
        # Avoid the trap of needing a serial path in TCP-gateway-only mode.
        with pytest.raises(ValueError, match="jkbms_path must be empty"):
            Settings(
                transport=Transport.TCP_GATEWAY,
                gateway_host="192.168.1.10",
                gateway_port=502,
                jkbms_path="/dev/ttyUSB0",
            )

    def test_usb_serial_requires_jkbms_path(self) -> None:
        with pytest.raises(ValueError, match="jkbms_path"):
            Settings(transport=Transport.USB_SERIAL)

    def test_usb_serial_with_gateway_host_fails(self) -> None:
        with pytest.raises(ValueError, match="gateway_host must be empty"):
            Settings(
                transport=Transport.USB_SERIAL,
                jkbms_path="/dev/ttyUSB0",
                gateway_host="x",
            )

    def test_usb_serial_with_empty_gateway_host_ok(self) -> None:
        # Exercises the False branch of `if self.gateway_host:` for USB_SERIAL.
        s = Settings(
            transport=Transport.USB_SERIAL,
            jkbms_path="/dev/ttyUSB0",
            gateway_host=None,
        )
        assert s.transport is Transport.USB_SERIAL

    def test_can_with_master_poll_fails(self) -> None:
        with pytest.raises(ValueError, match="not compatible"):
            Settings(transport=Transport.CAN_BUS, topology=Topology.MASTER_POLL)


class TestRanges:
    def test_jkbms_count_lower_bound(self) -> None:
        with pytest.raises(ValueError):
            Settings(
                transport=Transport.TCP_GATEWAY,
                gateway_host="a",
                gateway_port=502,
                jkbms_count=0,
            )

    def test_poll_interval_upper_bound(self) -> None:
        with pytest.raises(ValueError):
            Settings(
                transport=Transport.TCP_GATEWAY,
                gateway_host="a",
                gateway_port=502,
                poll_interval_s=31,
            )

    def test_inter_frame_gap_clamp(self) -> None:
        with pytest.raises(ValueError):
            Settings(
                transport=Transport.TCP_GATEWAY,
                gateway_host="a",
                gateway_port=502,
                inter_frame_gap_ms=5,
            )
        with pytest.raises(ValueError):
            Settings(
                transport=Transport.TCP_GATEWAY,
                gateway_host="a",
                gateway_port=502,
                inter_frame_gap_ms=600,
            )

    def test_log_level_validated(self) -> None:
        with pytest.raises(ValueError, match="log_level"):
            Settings(
                transport=Transport.TCP_GATEWAY,
                gateway_host="a",
                gateway_port=502,
                log_level="trace",
            )


class TestWriteAllowedByMode:
    def test_tcp_master_allows_writes(self) -> None:
        s = Settings(
            transport=Transport.TCP_GATEWAY,
            gateway_host="a",
            gateway_port=502,
            topology=Topology.MASTER_POLL,
        )
        assert s.writes_allowed_by_mode is True

    def test_tcp_broadcast_disallows_writes(self) -> None:
        s = Settings(
            transport=Transport.TCP_GATEWAY,
            gateway_host="a",
            gateway_port=502,
            topology=Topology.BROADCAST,
        )
        assert s.writes_allowed_by_mode is False


class TestLoadSettings:
    def test_load_from_options_json(self, tmp_path: Path) -> None:
        opts = {
            "transport": "tcp_gateway",
            "gateway_host": "10.0.0.10",
            "gateway_port": 502,
            "jkbms_count": 2,
            "poll_interval_s": 5,
        }
        p = tmp_path / "options.json"
        p.write_text(json.dumps(opts))
        s = load_settings(options_path=p, env={})
        assert s.gateway_host == "10.0.0.10"
        assert s.gateway_port == 502
        assert s.jkbms_count == 2

    def test_load_from_env(self, tmp_path: Path) -> None:
        env = {
            "JKBMS2MQTT_TRANSPORT": "tcp_gateway",
            "JKBMS2MQTT_GATEWAY_HOST": "1.2.3.4",
            "JKBMS2MQTT_GATEWAY_PORT": "8000",
            "JKBMS2MQTT_ENABLE_BASIC_WRITES": "true",
            "JKBMS2MQTT_POLL_INTERVAL_S": "2.5",
            "JKBMS2MQTT_LOG_LEVEL": "debug",
        }
        s = load_settings(options_path=tmp_path / "missing.json", env=env)
        assert s.gateway_host == "1.2.3.4"
        assert s.gateway_port == 8000
        assert s.enable_basic_writes is True
        assert s.poll_interval_s == 2.5
        assert s.log_level == "debug"

    def test_yaml_config_overrides_options(self, tmp_path: Path) -> None:
        opts = {
            "transport": "tcp_gateway",
            "gateway_host": "1.1.1.1",
            "gateway_port": 502,
        }
        opts_path = tmp_path / "options.json"
        opts_path.write_text(json.dumps(opts))
        yml = tmp_path / "extra.yaml"
        yml.write_text("gateway_host: 2.2.2.2\n")
        env = {"JKBMS2MQTT_CONFIG_YAML": str(yml)}
        s = load_settings(options_path=opts_path, env=env)
        assert s.gateway_host == "2.2.2.2"

    def test_env_overrides_yaml(self, tmp_path: Path) -> None:
        yml = tmp_path / "extra.yaml"
        yml.write_text(
            "transport: tcp_gateway\ngateway_host: 2.2.2.2\ngateway_port: 502\n"
        )
        env = {
            "JKBMS2MQTT_CONFIG_YAML": str(yml),
            "JKBMS2MQTT_GATEWAY_HOST": "3.3.3.3",
        }
        s = load_settings(options_path=tmp_path / "absent.json", env=env)
        assert s.gateway_host == "3.3.3.3"

    def test_options_must_be_object(self, tmp_path: Path) -> None:
        p = tmp_path / "options.json"
        p.write_text("[1,2,3]")
        with pytest.raises(ValueError, match="JSON object"):
            load_settings(options_path=p, env={})

    def test_yaml_must_be_mapping(self, tmp_path: Path) -> None:
        yml = tmp_path / "x.yaml"
        yml.write_text("- 1\n- 2\n")
        env = {
            "JKBMS2MQTT_CONFIG_YAML": str(yml),
            "JKBMS2MQTT_TRANSPORT": "tcp_gateway",
            "JKBMS2MQTT_GATEWAY_HOST": "1.1.1.1",
            "JKBMS2MQTT_GATEWAY_PORT": "502",
        }
        with pytest.raises(ValueError, match="YAML mapping"):
            load_settings(options_path=tmp_path / "absent.json", env=env)

    def test_default_options_path_and_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Exercises the `options_path is None` and `env is None` fallbacks."""
        from jkbms2mqtt import config as config_module

        # Point HA_OPTIONS_PATH at a non-existent file so we fall through to env.
        monkeypatch.setattr(config_module, "HA_OPTIONS_PATH", tmp_path / "absent.json")
        monkeypatch.setenv("JKBMS2MQTT_TRANSPORT", "tcp_gateway")
        monkeypatch.setenv("JKBMS2MQTT_GATEWAY_HOST", "via-env")
        monkeypatch.setenv("JKBMS2MQTT_GATEWAY_PORT", "1883")
        s = load_settings()  # no kwargs — defaults exercised
        assert s.gateway_host == "via-env"

    def test_env_var_without_prefix_is_ignored(self, tmp_path: Path) -> None:
        # The `continue` branch in _collect_env for non-prefixed env vars.
        env = {
            "PATH": "/usr/bin",  # not prefixed → must be skipped
            "JKBMS2MQTT_TRANSPORT": "tcp_gateway",
            "JKBMS2MQTT_GATEWAY_HOST": "x.x.x.x",
            "JKBMS2MQTT_GATEWAY_PORT": "502",
        }
        s = load_settings(options_path=tmp_path / "absent.json", env=env)
        assert s.gateway_host == "x.x.x.x"

    def test_empty_yaml_is_empty_dict(self, tmp_path: Path) -> None:
        yml = tmp_path / "empty.yaml"
        yml.write_text("")
        env = {
            "JKBMS2MQTT_CONFIG_YAML": str(yml),
            "JKBMS2MQTT_TRANSPORT": "tcp_gateway",
            "JKBMS2MQTT_GATEWAY_HOST": "1.1.1.1",
            "JKBMS2MQTT_GATEWAY_PORT": "502",
        }
        s = load_settings(options_path=tmp_path / "absent.json", env=env)
        assert s.gateway_host == "1.1.1.1"


class TestEnvValueCoercion:
    def test_bool(self) -> None:
        from jkbms2mqtt.config import _coerce_env_value

        assert _coerce_env_value("true") is True
        assert _coerce_env_value("false") is False
        assert _coerce_env_value("TRUE") is True

    def test_int(self) -> None:
        from jkbms2mqtt.config import _coerce_env_value

        assert _coerce_env_value("502") == 502
        assert _coerce_env_value("-1") == -1

    def test_float(self) -> None:
        from jkbms2mqtt.config import _coerce_env_value

        assert _coerce_env_value("2.5") == 2.5

    def test_string(self) -> None:
        from jkbms2mqtt.config import _coerce_env_value

        assert _coerce_env_value("hello") == "hello"


class TestBaudRateNotConfigurable:
    """Baud rate must not be user-configurable to prevent misconfiguration."""

    def test_settings_has_no_baud_field(self) -> None:
        with pytest.raises(ValueError):  # extra="forbid"
            Settings(
                transport=Transport.TCP_GATEWAY,
                gateway_host="x",
                gateway_port=502,
                baud_rate=115800,  # type: ignore[call-arg]
            )
