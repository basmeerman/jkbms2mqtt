"""Home Assistant MQTT Discovery + state publishing.

Generates HA Discovery payloads from the ``entities`` table, plus per-cycle
state publishing on the conventional JK-BMS topic suffixes. The discovery
``state_topic`` points at those same topics so dashboards / automations from
the legacy add-on work unchanged.

Two-tier write gating: write entities (``number`` / ``switch``) are only
advertised when the corresponding tier toggle in ``Settings`` is on. Posting
to a ``/set`` topic for a gated parameter never reaches the BMS — the
``write_executor`` refuses it and publishes a structured error on
``<bms_name>/error``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from jkbms2mqtt.config import Settings
from jkbms2mqtt.entities import (
    CELL_STATS_SENSORS,
    FIXED_SENSORS,
    LIVE_BINARY_SENSORS,
    LIVE_SENSORS,
    PACKED_BIT_ENTITIES,
    WRITABLE_ENTITIES,
    Component,
    PackedBitEntity,
    ReadOnlyEntity,
    WritableEntity,
    expand_cell_entities,
)
from jkbms2mqtt.protocol.jk_modbus import JkRealtime, JkStaticInfo
from jkbms2mqtt.protocol.jk_settings import WriteTier

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveryMessage:
    """One HA Discovery retained message."""

    topic: str
    payload: dict[str, Any]


# -- HA Discovery payload builders ----------------------------------------------------


def _device_info(bms_name: str) -> dict[str, Any]:
    """The HA ``device`` block used in every discovery payload.

    Matches the legacy convention: identifier ``BMS_<n>_device``, name
    ``BMS_<n>``. Existing automations / dashboards keyed on these IDs keep
    working.
    """
    return {
        "identifiers": [f"{bms_name}_device"],
        "name": bms_name,
        "manufacturer": "JIKONG",
        "model": "JK-BMS",
    }


def _state_topic(bms_name: str, suffix: str) -> str:
    return f"{bms_name}/{suffix}"


def _command_topic(bms_name: str, suffix: str) -> str:
    return f"{bms_name}/{suffix}/set"


def _discovery_topic(
    discovery_prefix: str, component: Component, bms_name: str, object_id: str
) -> str:
    return f"{discovery_prefix}/{component.value}/{bms_name}_device_{object_id}/config"


def discovery_for_read_only(
    entity: ReadOnlyEntity, bms_name: str, *, discovery_prefix: str
) -> DiscoveryMessage:
    unique_id = f"{bms_name}_device_{entity.object_id}"
    payload: dict[str, Any] = {
        "name": entity.description.rstrip("."),
        "state_topic": _state_topic(bms_name, entity.topic_suffix),
        "unique_id": unique_id,
        "object_id": unique_id,
        "device": _device_info(bms_name),
    }
    if entity.device_class:
        payload["device_class"] = entity.device_class
    if entity.state_class:
        payload["state_class"] = entity.state_class
    if entity.unit_of_measurement:
        payload["unit_of_measurement"] = entity.unit_of_measurement
    if entity.component is Component.BINARY_SENSOR:
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, entity.component, bms_name, entity.object_id),
        payload=payload,
    )


def discovery_for_writable(
    entity: WritableEntity, bms_name: str, *, discovery_prefix: str
) -> DiscoveryMessage:
    unique_id = f"{bms_name}_device_{entity.object_id}"
    payload: dict[str, Any] = {
        "name": entity.description.rstrip("."),
        "state_topic": _state_topic(bms_name, entity.topic_suffix),
        "command_topic": _command_topic(bms_name, entity.topic_suffix),
        "unique_id": unique_id,
        "object_id": unique_id,
        "device": _device_info(bms_name),
    }
    if entity.register.unit:
        payload["unit_of_measurement"] = entity.register.unit
    if entity.component is Component.NUMBER:
        payload["min"] = entity.register.min_value
        payload["max"] = entity.register.max_value
        payload["step"] = entity.register.step
        payload["mode"] = "box"
    if entity.component is Component.SWITCH:
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"
        payload["state_on"] = "ON"
        payload["state_off"] = "OFF"
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, entity.component, bms_name, entity.object_id),
        payload=payload,
    )


def discovery_for_packed_bit(
    entity: PackedBitEntity, bms_name: str, *, discovery_prefix: str
) -> DiscoveryMessage:
    unique_id = f"{bms_name}_device_{entity.object_id}"
    payload: dict[str, Any] = {
        "name": entity.bit.description.rstrip("."),
        "state_topic": _state_topic(bms_name, entity.topic_suffix),
        "command_topic": _command_topic(bms_name, entity.topic_suffix),
        "unique_id": unique_id,
        "object_id": unique_id,
        "device": _device_info(bms_name),
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
    }
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, entity.component, bms_name, entity.object_id),
        payload=payload,
    )


def build_discovery_messages(
    *,
    settings: Settings,
    bms_name: str,
    cell_count: int,
) -> list[DiscoveryMessage]:
    """Build every HA Discovery message appropriate for the current settings.

    Writable entities are only emitted when the matching tier toggle is on.
    """
    discovery_prefix = settings.discovery_prefix
    messages: list[DiscoveryMessage] = []

    for e in LIVE_SENSORS:
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in LIVE_BINARY_SENSORS:
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in CELL_STATS_SENSORS:
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in expand_cell_entities(cell_count):
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in FIXED_SENSORS:
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))

    for w in WRITABLE_ENTITIES:
        if w.register.tier is WriteTier.BASIC and not settings.enable_basic_writes:
            continue
        if w.register.tier is WriteTier.SAFETY and not settings.enable_safety_writes:
            continue
        messages.append(discovery_for_writable(w, bms_name, discovery_prefix=discovery_prefix))

    for p in PACKED_BIT_ENTITIES:
        if p.bit.tier is WriteTier.BASIC and not settings.enable_basic_writes:
            continue
        if (
            p.bit.tier is WriteTier.SAFETY and not settings.enable_safety_writes
        ):  # pragma: no cover - no SAFETY-tier packed bits today
            continue
        messages.append(discovery_for_packed_bit(p, bms_name, discovery_prefix=discovery_prefix))

    return messages


# -- State-message builders -----------------------------------------------------------


def state_messages_from_live(live: JkRealtime, bms_name: str) -> list[tuple[str, str]]:
    """Build ``(topic, payload)`` pairs for every live entity from a JkRealtime."""
    out: list[tuple[str, str]] = []

    for e in LIVE_SENSORS:
        value = getattr(live, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), _format(value)))
    for e in LIVE_BINARY_SENSORS:
        value = getattr(live, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), "ON" if value else "OFF"))
    for e in CELL_STATS_SENSORS:
        value = getattr(live, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), _format(value)))
    # Per-cell entities
    for i, v in enumerate(live.cell_voltages_v):
        out.append((f"{bms_name}/Cell_{i + 1}_volt", _format(v)))
    return out


def state_messages_from_static(info: JkStaticInfo, bms_name: str) -> list[tuple[str, str]]:
    """Build state messages for the static-info entities."""
    out: list[tuple[str, str]] = []
    for e in FIXED_SENSORS:
        value = getattr(info, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), _format(value)))
    return out


# -- Helpers --------------------------------------------------------------------------


def _format(value: object) -> str:
    """Format a value for an MQTT state topic."""
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, int):
        return str(value)
    return str(value)


def render(message: DiscoveryMessage) -> tuple[str, bytes]:
    """Serialise a discovery message for ``mqtt.publish``."""
    return message.topic, json.dumps(message.payload, separators=(",", ":")).encode()
