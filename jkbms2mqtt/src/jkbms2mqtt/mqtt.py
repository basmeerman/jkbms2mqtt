"""Home Assistant MQTT Discovery + state publisher.

Generates HA Discovery payloads from the `entities` table, plus per-frame state
publishing on the JK-BMS-conventional topic suffixes. The HA discovery
`state_topic` points at those same topics, so user-built dashboards keep
working *and* new HA installs auto-discover entities.

The discovery generator consults:
- `Settings.transport` and `Settings.topology` (via the capability matrix) to
  know whether writes are possible at all.
- `Settings.enable_basic_writes` and `Settings.enable_safety_writes` to know
  which writable entities to advertise.

In modes where writes are impossible (broadcast, CAN), no `number`/`switch`/`select`
discovery is published, no matter what the toggles say. That's the hard-refuse
policy from the plan.
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
from jkbms2mqtt.protocol.registers import Encoding, WriteTier

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiscoveryMessage:
    """One HA Discovery retained message: topic + payload."""

    topic: str
    payload: dict[str, Any]


def _device_info(bms_name: str, *, slug: str) -> dict[str, Any]:
    return {
        "identifiers": [slug],
        "name": bms_name,
        "manufacturer": "JIKONG",
        "model": "JK-BMS",
        "via_device": "jkbms2mqtt",
    }


def _state_topic(bms_name: str, suffix: str) -> str:
    return f"{bms_name}/{suffix}"


def _command_topic(bms_name: str, suffix: str) -> str:
    return f"{bms_name}/{suffix}/set"


def discovery_for_read_only(
    entity: ReadOnlyEntity, bms_name: str, *, slug: str, discovery_prefix: str
) -> DiscoveryMessage:
    """Build the discovery message for a read-only sensor / binary_sensor."""
    object_id = f"{slug}_{entity.object_id}"
    topic = (
        f"{discovery_prefix}/{entity.component.value}/{slug}/{entity.object_id}/config"
    )
    payload: dict[str, Any] = {
        "name": entity.description.rstrip("."),
        "state_topic": _state_topic(bms_name, entity.topic_suffix),
        "unique_id": object_id,
        "object_id": object_id,
        "device": _device_info(bms_name, slug=slug),
    }
    if entity.device_class:
        payload["device_class"] = entity.device_class
    if entity.state_class:
        payload["state_class"] = entity.state_class
    if entity.unit_of_measurement:
        payload["unit_of_measurement"] = entity.unit_of_measurement
    if entity.component is Component.BINARY_SENSOR:
        # HA's binary sensors expect a payload mapping for on/off.
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"
    return DiscoveryMessage(topic=topic, payload=payload)


def discovery_for_writable(
    entity: WritableEntity, bms_name: str, *, slug: str, discovery_prefix: str
) -> DiscoveryMessage:
    object_id = f"{slug}_{entity.object_id}"
    topic = (
        f"{discovery_prefix}/{entity.component.value}/{slug}/{entity.object_id}/config"
    )
    payload: dict[str, Any] = {
        "name": entity.description.rstrip("."),
        "state_topic": _state_topic(bms_name, entity.topic_suffix),
        "command_topic": _command_topic(bms_name, entity.topic_suffix),
        "unique_id": object_id,
        "object_id": object_id,
        "device": _device_info(bms_name, slug=slug),
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
    return DiscoveryMessage(topic=topic, payload=payload)


def discovery_for_packed_bit(
    entity: PackedBitEntity, bms_name: str, *, slug: str, discovery_prefix: str
) -> DiscoveryMessage:
    object_id = f"{slug}_{entity.object_id}"
    topic = (
        f"{discovery_prefix}/{entity.component.value}/{slug}/{entity.object_id}/config"
    )
    payload: dict[str, Any] = {
        "name": entity.bit.description.rstrip("."),
        "state_topic": _state_topic(bms_name, entity.topic_suffix),
        "command_topic": _command_topic(bms_name, entity.topic_suffix),
        "unique_id": object_id,
        "object_id": object_id,
        "device": _device_info(bms_name, slug=slug),
        "payload_on": "ON",
        "payload_off": "OFF",
        "state_on": "ON",
        "state_off": "OFF",
    }
    return DiscoveryMessage(topic=topic, payload=payload)


def build_discovery_messages(
    *,
    settings: Settings,
    bms_name: str,
    cell_count: int,
    slug: str | None = None,
) -> list[DiscoveryMessage]:
    """Build every HA Discovery message appropriate for the current mode + toggles.

    The single decision point that enforces the "no impossible-write entities"
    policy. Callers should treat this as the source of truth and publish exactly
    what it returns.
    """
    if slug is None:
        slug = f"jkbms_{bms_name}".lower().replace(" ", "_")
    discovery_prefix = settings.discovery_prefix
    messages: list[DiscoveryMessage] = []

    # Always: every read-only entity.
    read_only_entities: tuple[ReadOnlyEntity, ...] = (
        LIVE_SENSORS
        + LIVE_BINARY_SENSORS
        + CELL_STATS_SENSORS
        + expand_cell_entities(cell_count)
        + FIXED_SENSORS
    )
    for e in read_only_entities:
        messages.append(
            discovery_for_read_only(
                e, bms_name, slug=slug, discovery_prefix=discovery_prefix
            )
        )

    if not settings.writes_allowed_by_mode:
        return messages

    # Writable entities: gated by tier toggles.
    for w in WRITABLE_ENTITIES:
        if w.register.tier is WriteTier.BASIC and not settings.enable_basic_writes:
            continue
        if w.register.tier is WriteTier.SAFETY and not settings.enable_safety_writes:
            continue
        messages.append(
            discovery_for_writable(
                w, bms_name, slug=slug, discovery_prefix=discovery_prefix
            )
        )

    for p in PACKED_BIT_ENTITIES:
        if p.bit.tier is WriteTier.BASIC and not settings.enable_basic_writes:
            continue
        if (
            p.bit.tier is WriteTier.SAFETY
            and not settings.enable_safety_writes
        ):  # pragma: no cover - no SAFETY-tier packed bits exist today; guard kept for future
            continue
        messages.append(
            discovery_for_packed_bit(
                p, bms_name, slug=slug, discovery_prefix=discovery_prefix
            )
        )

    return messages


# ----- per-frame state publishing ------------------------------------------------------------


def state_messages_from_live(
    live: object, bms_name: str
) -> list[tuple[str, str]]:
    """Build `(topic, payload)` pairs for every live-data entity from a `LiveData` instance.

    Numeric values are stringified with HA-friendly precision. Booleans become "ON"/"OFF"
    for binary sensors.
    """
    from jkbms2mqtt.protocol.decoder import LiveData

    assert isinstance(live, LiveData)
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
    cells = live.cell_voltages_v
    for i, v in enumerate(cells):
        out.append((f"{bms_name}/Cell_{i + 1}_volt", _format(v)))
    resistances = live.cell_resistances_ohm
    for i, r in enumerate(resistances):
        out.append((f"{bms_name}/Cell_{i + 1}_ohm", _format(r)))
    return out


def state_messages_from_setup(setup: object, bms_name: str) -> list[tuple[str, str]]:
    """Echo each writable parameter's CURRENT BMS-reported value to its state topic.

    This lets HA show the right initial value before any user write.
    """
    from jkbms2mqtt.protocol.decoder import SetupData

    assert isinstance(setup, SetupData)
    out: list[tuple[str, str]] = []
    for w in WRITABLE_ENTITIES:
        source = _setup_field_name(w)
        if not hasattr(setup, source):  # pragma: no cover - all current entities map cleanly
            continue
        value = getattr(setup, source)
        if w.register.encoding is Encoding.BOOL32:
            payload = "ON" if value else "OFF"
        else:
            payload = _format(value)
        out.append((_state_topic(bms_name, w.topic_suffix), payload))
    for p in PACKED_BIT_ENTITIES:
        value = getattr(setup, p.bit.name)
        out.append(
            (_state_topic(bms_name, p.topic_suffix), "ON" if value else "OFF")
        )
    return out


def state_messages_from_fixed(fixed: object, bms_name: str) -> list[tuple[str, str]]:
    """Build state messages for the static / device-info topics."""
    from jkbms2mqtt.protocol.decoder import FixedData

    assert isinstance(fixed, FixedData)
    out: list[tuple[str, str]] = []
    for e in FIXED_SENSORS:
        value = getattr(fixed, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), _format(value)))
    return out


def _setup_field_name(entity: WritableEntity) -> str:
    obj_id = entity.object_id
    # The decoder's SetupData uses these naming conventions:
    # - voltages: <name>_v
    # - currents: <name>_a
    # - durations: <name>_s
    # - temperatures: <name>_c
    # - capacity: <name>_ah
    # - counts / addresses: <name>  (no suffix)
    # - booleans: <name>            (no suffix)
    suffix_map = {
        "V": "_v",
        "A": "_a",
        "s": "_s",
        "°C": "_c",
        "Ah": "_ah",
    }
    suffix = suffix_map.get(entity.register.unit or "", "")
    return obj_id + suffix


def _format(value: object) -> str:
    """Format a value for an MQTT state topic.

    Booleans become 'ON'/'OFF', floats are rendered with a trailing newline-free,
    HA-friendly precision (3 dp), ints stay as int strings, strings pass through.
    """
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, int):
        return str(value)
    return str(value)


def render(message: DiscoveryMessage) -> tuple[str, bytes]:
    """Serialize a discovery message for `aiomqtt.publish`."""
    return message.topic, json.dumps(message.payload, separators=(",", ":")).encode()
