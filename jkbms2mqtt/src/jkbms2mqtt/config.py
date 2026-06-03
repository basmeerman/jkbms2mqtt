"""Configuration model for the add-on.

Loads settings from one of three sources, in priority order (later overrides
earlier):

1. ``/data/options.json`` (Home Assistant add-on convention).
2. A YAML file pointed to by ``JKBMS2MQTT_CONFIG_YAML`` (standalone Docker).
3. ``JKBMS2MQTT_*`` environment variables (also standalone Docker).

The schema mirrors the HA add-on ``config.yaml``. Every field is **required**
with a default, so the HA Configuration tab does not show the "Show unused
optional configuration options" toggle.
"""

from __future__ import annotations

import json
import os
from enum import Enum, unique
from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HA_OPTIONS_PATH = Path("/data/options.json")


@unique
class Transport(str, Enum):
    TCP_GATEWAY = "tcp_gateway"
    USB_SERIAL = "usb_serial"


@unique
class Topology(str, Enum):
    MASTER_POLL = "master_poll"


@unique
class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Settings(BaseModel):
    """Run-time configuration. Frozen, validated, all-fields-required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transport: Transport = Transport.TCP_GATEWAY
    gateway_host: str = "192.168.1.100"
    gateway_port: Annotated[int, Field(ge=1, le=65535)] = 502
    jkbms_path: str = "/dev/ttyUSB0"
    bms_ids: list[Annotated[int, Field(ge=1, le=15)]] = Field(default_factory=lambda: [1])
    topology: Topology = Topology.MASTER_POLL
    poll_interval_s: Annotated[float, Field(ge=1.0, le=60.0)] = 5.0

    mqtt_host: str = "core-mosquitto.local.hass.io"
    mqtt_port: Annotated[int, Field(ge=1, le=65535)] = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    discovery_prefix: str = "homeassistant"
    bms_name_prefix: str = "BMS"

    enable_basic_writes: bool = False
    enable_safety_writes: bool = False

    # Surface entities whose Modbus offset has not yet been confirmed against
    # real hardware. Off by default — these entities would otherwise publish
    # plausibly-shaped but unverified values.
    debug_unverified_fields: bool = False

    log_level: LogLevel = LogLevel.INFO
    recording_enabled: bool = False

    @field_validator("bms_ids", mode="before")
    @classmethod
    def _parse_bms_ids(cls, v: object) -> object:
        """Accept either a list of ints or a comma-separated string.

        Home Assistant's add-on UI renders a regex-validated ``str`` field
        as a single text input — easier and more reliable across HA versions
        than the list editor. The Python side accepts both forms so YAML
        configs, JSON options, and the ``JKBMS2MQTT_BMS_IDS`` env var all
        work uniformly.
        """
        if isinstance(v, str):
            return [int(s.strip()) for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.transport is Transport.TCP_GATEWAY and not self.gateway_host:
            raise ValueError("transport=tcp_gateway requires a non-empty gateway_host")
        if self.transport is Transport.USB_SERIAL and not self.jkbms_path:
            raise ValueError("transport=usb_serial requires a non-empty jkbms_path")
        if not self.bms_ids:
            raise ValueError("bms_ids must contain at least one slave address")
        if len(self.bms_ids) != len(set(self.bms_ids)):
            raise ValueError(f"bms_ids contains duplicates: {self.bms_ids}")
        if not self.bms_name_prefix:
            raise ValueError("bms_name_prefix must not be empty")
        return self


def load_settings(
    *,
    options_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    """Load settings from ``/data/options.json``, YAML, and env vars."""
    if options_path is None:
        options_path = HA_OPTIONS_PATH
    if env is None:
        env = dict(os.environ)

    raw: dict[str, object] = {}

    if options_path.is_file():
        raw.update(_load_options_json(options_path))

    yaml_path = env.get("JKBMS2MQTT_CONFIG_YAML")
    if yaml_path:
        raw.update(_load_yaml(Path(yaml_path)))

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
_ENV_SKIP = frozenset({"CONFIG_YAML"})


def _collect_env(env: dict[str, str]) -> dict[str, object]:
    """Translate ``JKBMS2MQTT_FOO_BAR`` env vars into ``{foo_bar: ...}``.

    ``bms_ids`` is left as a string here — the Settings field validator
    handles the comma-split parsing uniformly for env, YAML, and HA-options
    inputs.
    """
    out: dict[str, object] = {}
    for key, value in env.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        suffix = key[len(_ENV_PREFIX) :]
        if suffix in _ENV_SKIP:
            continue
        field = suffix.lower()
        if field == "bms_ids":
            out[field] = value  # parsed by Settings._parse_bms_ids
        else:
            out[field] = _coerce_env_value(value)
    return out


def _coerce_env_value(value: str) -> bool | int | float | str:
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
