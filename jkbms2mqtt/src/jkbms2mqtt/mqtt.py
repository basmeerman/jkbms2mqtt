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
from jkbms2mqtt.protocol.jk_settings import (
    Encoding,
    PackedBitDef,
    RegisterDef,
    WriteTier,
)

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
    if entity.decimals is not None:
        # Tell HA's frontend how many decimal places to render. Without this,
        # HA picks a device-class-specific default (often 1 for voltage), which
        # truncates millivolt-resolution cell readings down to ``3 V``.
        payload["suggested_display_precision"] = entity.decimals
    if entity.component is Component.BINARY_SENSOR:
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"
    if entity.entity_category is not None:
        payload["entity_category"] = entity.entity_category
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, entity.component, bms_name, entity.object_id),
        payload=payload,
    )


def discovery_for_writable(
    entity: WritableEntity, bms_name: str, *, discovery_prefix: str, writable: bool
) -> DiscoveryMessage:
    """Discovery payload for a settable parameter.

    When ``writable`` is True the entity is published as a ``number``/``switch``
    (HA shows controls). When False it is published as ``sensor``/
    ``binary_sensor`` (status only). Either way the same state topic carries
    the current BMS value.
    """
    unique_id = f"{bms_name}_device_{entity.object_id}"
    is_bool = entity.register.encoding is Encoding.BOOL32
    if writable:
        component = Component.SWITCH if is_bool else Component.NUMBER
    else:
        component = Component.BINARY_SENSOR if is_bool else Component.SENSOR

    payload: dict[str, Any] = {
        "name": entity.description.rstrip("."),
        "state_topic": _state_topic(bms_name, entity.topic_suffix),
        "unique_id": unique_id,
        "object_id": unique_id,
        "device": _device_info(bms_name),
    }
    if writable:
        payload["command_topic"] = _command_topic(bms_name, entity.topic_suffix)
    if entity.register.unit:
        payload["unit_of_measurement"] = entity.register.unit
        # Match the precision the BMS encoding stores — see jk_settings.Encoding.
        decimals = _decimals_for_encoding(entity.register.encoding)
        if decimals is not None:
            payload["suggested_display_precision"] = decimals
    if component is Component.NUMBER:
        payload["min"] = entity.register.min_value
        payload["max"] = entity.register.max_value
        payload["step"] = entity.register.step
        payload["mode"] = "box"
    if component in (Component.SWITCH, Component.BINARY_SENSOR):
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"
    if component is Component.SWITCH:
        payload["state_on"] = "ON"
        payload["state_off"] = "OFF"
    if entity.entity_category is not None:
        payload["entity_category"] = entity.entity_category
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, component, bms_name, entity.object_id),
        payload=payload,
    )


def discovery_for_packed_bit(
    entity: PackedBitEntity, bms_name: str, *, discovery_prefix: str, writable: bool
) -> DiscoveryMessage:
    """Discovery for a packed-bit boolean — switch when writable, binary sensor otherwise."""
    unique_id = f"{bms_name}_device_{entity.object_id}"
    component = Component.SWITCH if writable else Component.BINARY_SENSOR
    payload: dict[str, Any] = {
        "name": entity.bit.description.rstrip("."),
        "state_topic": _state_topic(bms_name, entity.topic_suffix),
        "unique_id": unique_id,
        "object_id": unique_id,
        "device": _device_info(bms_name),
        "payload_on": "ON",
        "payload_off": "OFF",
    }
    if writable:
        payload["command_topic"] = _command_topic(bms_name, entity.topic_suffix)
        payload["state_on"] = "ON"
        payload["state_off"] = "OFF"
    if entity.entity_category is not None:
        payload["entity_category"] = entity.entity_category
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, component, bms_name, entity.object_id),
        payload=payload,
    )


def _decimals_for_encoding(encoding: Encoding) -> int | None:
    """Decimals matching the BMS's native scale for the encoding."""
    if encoding is Encoding.U32_MILLI:
        return 3
    if encoding is Encoding.U32_DECI or encoding is Encoding.I32_DECI:
        return 1
    return None


def build_discovery_messages(
    *,
    settings: Settings,
    bms_name: str,
    cell_count: int,
) -> list[DiscoveryMessage]:
    """Build every HA Discovery message appropriate for the current settings.

    Writable entities are only emitted when the matching tier toggle is on.
    Entities flagged ``verified=False`` are skipped unless
    ``settings.debug_unverified_fields`` is True.
    """
    discovery_prefix = settings.discovery_prefix
    debug = settings.debug_unverified_fields
    messages: list[DiscoveryMessage] = []

    for e in LIVE_SENSORS:
        if not e.verified and not debug:
            continue
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in LIVE_BINARY_SENSORS:
        if not e.verified and not debug:
            continue
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in CELL_STATS_SENSORS:
        # No CELL_STATS entity is currently unverified; this defensive check
        # exists for future additions.
        if not e.verified and not debug:  # pragma: no branch
            continue  # pragma: no cover
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in expand_cell_entities(cell_count):
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in FIXED_SENSORS:
        if not e.verified and not debug:  # pragma: no branch - no unverified FIXED entries today
            continue  # pragma: no cover
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))

    for w in WRITABLE_ENTITIES:
        if not w.verified and not debug:  # pragma: no branch - no unverified writables today
            continue  # pragma: no cover
        writable = _tier_enabled(settings, w.register.tier)
        messages.append(
            discovery_for_writable(
                w, bms_name, discovery_prefix=discovery_prefix, writable=writable
            )
        )

    for p in PACKED_BIT_ENTITIES:
        if not p.verified and not debug:
            continue
        writable = _tier_enabled(settings, p.bit.tier)
        messages.append(
            discovery_for_packed_bit(
                p, bms_name, discovery_prefix=discovery_prefix, writable=writable
            )
        )

    return messages


def _tier_enabled(settings: Settings, tier: WriteTier) -> bool:
    if tier is WriteTier.BASIC:
        return settings.enable_basic_writes
    return settings.enable_safety_writes


# -- State-message builders -----------------------------------------------------------


def state_messages_from_live(
    live: JkRealtime, bms_name: str, *, debug_unverified: bool = False
) -> list[tuple[str, str]]:
    """Build ``(topic, payload)`` pairs for every live entity from a JkRealtime.

    Unverified entities are skipped unless ``debug_unverified`` is True.
    """
    out: list[tuple[str, str]] = []

    for e in LIVE_SENSORS:
        if not e.verified and not debug_unverified:
            continue
        value = getattr(live, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), _format(value, e.decimals)))
    for e in LIVE_BINARY_SENSORS:
        if not e.verified and not debug_unverified:
            continue
        value = getattr(live, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), "ON" if value else "OFF"))
    for e in CELL_STATS_SENSORS:
        # See build_discovery_messages for rationale.
        if not e.verified and not debug_unverified:  # pragma: no branch
            continue  # pragma: no cover
        value = getattr(live, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), _format(value, e.decimals)))
    # Per-cell entities — mV-resolution voltages and mΩ-resolution resistances.
    for i, v in enumerate(live.cell_voltages_v):
        out.append((f"{bms_name}/Cell_{i + 1}_volt", _format(v, 3)))
    for i, r in enumerate(live.cell_resistances_ohm):
        out.append((f"{bms_name}/Cell_{i + 1}_ohm", _format(r, 3)))
    return out


def state_messages_from_static(info: JkStaticInfo, bms_name: str) -> list[tuple[str, str]]:
    """Build state messages for the static-info entities."""
    out: list[tuple[str, str]] = []
    for e in FIXED_SENSORS:
        value = getattr(info, e.source_field)
        out.append((_state_topic(bms_name, e.topic_suffix), _format(value, e.decimals)))
    return out


def state_messages_from_settings(
    *,
    register_values: dict[RegisterDef, float | bool],
    packed_values: dict[PackedBitDef, bool],
    bms_name: str,
    debug_unverified: bool = False,
) -> list[tuple[str, str]]:
    """Build state messages for the BMS's current settings.

    Lets HA display the *current* value of every writable parameter even when
    its write tier is disabled (entity is published as a sensor in that case).
    Unverified entities are skipped unless ``debug_unverified`` is True.
    """
    out: list[tuple[str, str]] = []
    for w in WRITABLE_ENTITIES:
        if not w.verified and not debug_unverified:  # pragma: no branch - no unverified writables today
            continue  # pragma: no cover
        if w.register not in register_values:
            continue
        value = register_values[w.register]
        topic = _state_topic(bms_name, w.topic_suffix)
        if w.register.encoding is Encoding.BOOL32:  # pragma: no branch - no BOOL32 regs today
            out.append((topic, "ON" if value else "OFF"))  # pragma: no cover
        else:
            decimals = _decimals_for_encoding(w.register.encoding)
            out.append((topic, _format(value, decimals)))
    for p in PACKED_BIT_ENTITIES:
        if not p.verified and not debug_unverified:
            continue
        if p.bit not in packed_values:
            continue
        topic = _state_topic(bms_name, p.topic_suffix)
        out.append((topic, "ON" if packed_values[p.bit] else "OFF"))
    return out


# -- Helpers --------------------------------------------------------------------------


def _format(value: object, decimals: int | None = None) -> str:
    """Format a value for an MQTT state topic.

    ``decimals`` controls how many decimal places a numeric value gets:

    - ``None`` defaults to 3 for floats (preserves the old behaviour for
      values without an explicit precision).
    - ``0`` renders floats as integer strings (``"3"``, not ``"3.000"``).
    - Any other value renders with that many decimal places.

    Booleans and strings are unaffected.
    """
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        if decimals is None:
            decimals = 3
        if decimals == 0:
            return str(int(round(value)))
        return f"{value:.{decimals}f}"
    if isinstance(value, int):
        return str(value)
    return str(value)


def render(message: DiscoveryMessage) -> tuple[str, bytes]:
    """Serialise a discovery message for ``mqtt.publish``."""
    return message.topic, json.dumps(message.payload, separators=(",", ":")).encode()
