"""Configuration model.

Loads from one of three sources (in priority order):
1. `/data/options.json` (Home Assistant add-on convention).
2. `--config /path/to.yaml` (standalone).
3. `JKBMS2MQTT_*` environment variables (Docker / dev).

Pydantic v2 validators encode the cross-field policy:
- `transport == tcp_gateway`     ⇒ gateway_host + gateway_port required.
- `transport == usb_serial`      ⇒ jkbms_path required.
- `transport == can_bus`         ⇒ topology forced to `can`; writes forced off.
- `topology == broadcast`        ⇒ writes forced off.

Baud rate is not user-configurable — hardcoded to 115200 to prevent misconfiguration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jkbms2mqtt.protocol.capabilities import Topology, Transport, is_valid_combo

HA_OPTIONS_PATH = Path("/data/options.json")


class RecordingSettings(BaseModel):
    """Optional traffic recorder — appends every byte to JSONL for replay-based tests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    path: str = "/share/jkbms2mqtt/recordings"


class Settings(BaseModel):
    """All runtime configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Transport = Transport.TCP_GATEWAY
    gateway_host: str | None = None
    gateway_port: Annotated[int, Field(ge=1, le=65535)] | None = None
    jkbms_path: str | None = None
    jkbms_count: Annotated[int, Field(ge=1, le=15)] = 1
    topology: Topology = Topology.MASTER_POLL
    poll_interval_s: Annotated[float, Field(ge=1.0, le=30.0)] = 3.0
    inter_frame_gap_ms: Annotated[int, Field(ge=10, le=500)] = 50

    mqtt_host: str = "core-mosquitto"
    mqtt_port: Annotated[int, Field(ge=1, le=65535)] = 1883
    mqtt_user: str | None = None
    mqtt_password: str | None = None
    mqtt_topic_prefix: str = ""
    discovery_prefix: str = "homeassistant"

    enable_basic_writes: bool = False
    enable_safety_writes: bool = False

    log_level: str = "info"
    recording: RecordingSettings = Field(default_factory=RecordingSettings)

    @model_validator(mode="after")
    def _validate_transport_combo(self) -> Self:
        if not is_valid_combo(self.transport, self.topology):
            raise ValueError(
                f"transport={self.transport.value} is not compatible with "
                f"topology={self.topology.value}"
            )

        if self.transport is Transport.TCP_GATEWAY:
            if not self.gateway_host or self.gateway_port is None:
                raise ValueError(
                    "transport=tcp_gateway requires gateway_host and gateway_port"
                )
            if self.jkbms_path:
                raise ValueError(
                    "transport=tcp_gateway: jkbms_path must be empty"
                )

        if self.transport is Transport.USB_SERIAL:
            if not self.jkbms_path:
                raise ValueError(
                    "transport=usb_serial requires jkbms_path"
                )
            if self.gateway_host:
                raise ValueError(
                    "transport=usb_serial: gateway_host must be empty"
                )

        if self.log_level not in ("debug", "info", "warning", "error"):
            raise ValueError(
                f"log_level must be one of debug/info/warning/error, got {self.log_level!r}"
            )

        return self

    @property
    def writes_allowed_by_mode(self) -> bool:
        """True iff the active (transport, topology) supports writes.

        The constructor's validator already guarantees `(transport, topology)` is a
        configured combo, so the lookup always returns a `Capabilities` instance.
        """
        from jkbms2mqtt.protocol.capabilities import lookup

        caps = lookup(self.transport, self.topology)
        assert caps is not None  # validator invariant
        return caps.writes


def load_settings(*, options_path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    """Load settings from HA options.json (if present), YAML, or environment.

    *options_path* is for tests — the default behaviour is to read
    `/data/options.json` if it exists, otherwise fall back to environment variables.
    """
    if options_path is None:
        options_path = HA_OPTIONS_PATH
    if env is None:
        env = dict(os.environ)

    raw: dict[str, object] = {}

    if options_path.is_file():
        raw.update(_load_options_json(options_path))

    yaml_path_str = env.get("JKBMS2MQTT_CONFIG_YAML")
    if yaml_path_str:
        raw.update(_load_yaml(Path(yaml_path_str)))

    raw.update(_collect_env(env))

    return Settings.model_validate(raw)


def _load_options_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at top level")
    return data


def _load_yaml(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at top level")
    return data


_ENV_PREFIX = "JKBMS2MQTT_"

# fields that are NOT readable from the env (config file path itself, plus
# nested objects we'd need bespoke parsing for)
_ENV_SKIP = frozenset({"CONFIG_YAML", "RECORDING"})


def _collect_env(env: dict[str, str]) -> dict[str, object]:
    """Map env vars `JKBMS2MQTT_FOO_BAR=baz` → `{"foo_bar": "baz"}`."""
    out: dict[str, object] = {}
    for key, value in env.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        suffix = key[len(_ENV_PREFIX) :]
        if suffix in _ENV_SKIP:
            continue
        field = suffix.lower()
        out[field] = _coerce_env_value(value)
    return out


def _coerce_env_value(value: str) -> bool | int | float | str:
    """Coerce env string into the natural type pydantic expects."""
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    # numeric? try int first to avoid 5.0 trip on "5"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
