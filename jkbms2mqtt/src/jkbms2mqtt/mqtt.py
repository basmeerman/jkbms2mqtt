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
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from jkbms2mqtt.config import Settings
from jkbms2mqtt.entities import (
    BRIDGE_DEVICE_ID,
    BRIDGE_DEVICE_NAME,
    BRIDGE_SENSORS,
    BRIDGE_TIER_SENSORS,
    CELL_STATS_SENSORS,
    FIXED_SENSORS,
    LIVE_BINARY_SENSORS,
    LIVE_SENSORS,
    PACKED_BIT_ENTITIES,
    WRITABLE_ENTITIES,
    BridgeTierSensor,
    Component,
    PackedBitEntity,
    ReadOnlyEntity,
    WritableEntity,
    control_name,
    control_object_id,
    expand_cell_entities,
    writable_component,
)
from jkbms2mqtt.protocol.jk_modbus import MAX_CELLS, JkRealtime, JkStaticInfo
from jkbms2mqtt.protocol.jk_settings import (
    Encoding,
    PackedBitDef,
    RegisterDef,
    WriteTier,
)

logger = logging.getLogger(__name__)

# Retained LWT for the bridge process: ``online`` on connect, ``offline`` when
# the MQTT session dies. Payloads match HA's availability defaults.
BRIDGE_AVAILABILITY_TOPIC = "jkbms2mqtt/availability"

def tier_topic(tier: WriteTier) -> str:
    """Retained topic carrying ``online`` / ``offline`` for one write tier.

    Serves two purposes at once: it is the state topic of that tier's bridge
    sensor, and an availability topic of every control the tier gates — so a
    control is operable exactly when the tier says so.
    """
    return f"jkbms2mqtt/{tier.value}_writes"


@dataclass(frozen=True, slots=True)
class DiscoveryMessage:
    """One HA Discovery retained message.

    ``payload=None`` is a removal: it renders as an empty retained message,
    which clears the broker's retained config and makes HA delete the entity.
    """

    topic: str
    payload: dict[str, Any] | None


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


def _bridge_device_info() -> dict[str, Any]:
    """The HA ``device`` block for the bridge itself."""
    return {
        "identifiers": [BRIDGE_DEVICE_ID],
        "name": BRIDGE_DEVICE_NAME,
        "manufacturer": "jkbms2mqtt",
        "model": "JK-BMS to MQTT bridge",
    }


def _state_topic(bms_name: str, suffix: str) -> str:
    return f"{bms_name}/{suffix}"


def _command_topic(bms_name: str, suffix: str) -> str:
    return f"{bms_name}/{suffix}/set"


def _discovery_topic(
    discovery_prefix: str, component: Component, bms_name: str, object_id: str
) -> str:
    return f"{discovery_prefix}/{component.value}/{bms_name}_device_{object_id}/config"


def discovery_removal(
    discovery_prefix: str, component: Component, bms_name: str, object_id: str
) -> DiscoveryMessage:
    """Empty retained config for a topic this bridge may have published before.

    The component is part of the discovery topic, so an entity that changes
    component (tier toggled), disappears (fewer cells) or gets hidden (debug
    flag off) leaves its old retained config behind. Publishing an empty
    retained payload there clears it; for a topic that never existed this is
    a no-op for both broker and HA.
    """
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, component, bms_name, object_id),
        payload=None,
    )


def orphan_removals(topics: Iterable[str], *, settings: Settings) -> list[DiscoveryMessage]:
    """Removals for retained discovery configs of packs this bridge no longer polls.

    ``topics`` is what the broker replayed under
    ``<discovery_prefix>/+/+/config``. A topic is cleared only when it is
    unmistakably ours — our discovery prefix, a known component, and a device
    id shaped ``<bms_name_prefix>_<slave id>_device_<object_id>`` — and its
    slave id is **not** in ``bms_ids``. Everything else is left alone: other
    integrations, another jkbms2mqtt instance with a different prefix, and the
    packs we still poll (whose own removals ``build_discovery_messages``
    already handles).

    Pure on purpose. What gets deleted is decided here, under test; the
    subscribe / collect side is glue in ``app.run``.
    """
    pattern = re.compile(
        rf"^{re.escape(settings.discovery_prefix)}/(?P<component>[^/]+)/"
        rf"{re.escape(settings.bms_name_prefix)}_(?P<slave>\d+)_device_[^/]+/config$"
    )
    components = {c.value for c in Component}
    configured = set(settings.bms_ids)

    out: list[DiscoveryMessage] = []
    for topic in topics:
        match = pattern.match(topic)
        if match is None or match["component"] not in components:
            continue
        if int(match["slave"]) in configured:
            continue
        # Clear the topic exactly as the broker reported it, rather than
        # rebuilding it from an object_id we may no longer know.
        out.append(DiscoveryMessage(topic=topic, payload=None))
    return out


def _read_only_category(entity_category: str | None) -> str | None:
    """Category for a setting published read-only because its tier is off.

    HA refuses to add a sensor / binary_sensor with ``entity_category:
    config`` ("cannot be added as the entity category is set to config"), so
    the read-only mirror of a configuration entity goes to Diagnostics.
    """
    return "diagnostic" if entity_category == "config" else entity_category


def _base_payload(
    bms_name: str,
    *,
    component: Component,
    object_id: str,
    name: str,
    topic_suffix: str,
    entity_category: str | None,
    follows_bridge_availability: bool = True,
) -> dict[str, Any]:
    """The fields every discovery payload shares, regardless of component.

    ``entity_category`` is emitted only when set (HA treats an absent key as
    "primary entity"), so passing ``None`` leaves it off the payload. Every
    ``discovery_for_*`` builder starts from this so the common shape — and the
    optional ``entity_category`` — lives in exactly one place.

    No entity id is suggested. MQTT entities get ``has_entity_name`` True, so
    HA derives ``<domain>.<device name>_<entity name>`` from the device name
    and ``name`` — the documented convention
    (https://developers.home-assistant.io/docs/core/entity/#entity-naming),
    and HA core warns that "in most cases, entities should not set entity_id".
    ``object_id`` was removed from MQTT discovery in HA 2026.4; its successor
    ``default_entity_id`` is an override this bridge deliberately does not use.
    """
    payload: dict[str, Any] = {
        "name": name.rstrip("."),
        "state_topic": _state_topic(bms_name, topic_suffix),
        "unique_id": f"{bms_name}_device_{object_id}",
        "device": _device_info(bms_name),
    }
    if follows_bridge_availability:
        payload["availability_topic"] = BRIDGE_AVAILABILITY_TOPIC
    if entity_category is not None:
        payload["entity_category"] = entity_category
    return payload


def discovery_for_read_only(
    entity: ReadOnlyEntity, bms_name: str, *, discovery_prefix: str
) -> DiscoveryMessage:
    payload = _base_payload(
        bms_name,
        component=entity.component,
        object_id=entity.object_id,
        name=entity.description,
        topic_suffix=entity.topic_suffix,
        entity_category=entity.entity_category,
        follows_bridge_availability=entity.follows_bridge_availability,
    )
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
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, entity.component, bms_name, entity.object_id),
        payload=payload,
    )


def discovery_for_writable(
    entity: WritableEntity, bms_name: str, *, discovery_prefix: str
) -> DiscoveryMessage:
    """Read-only view of a settable parameter, published whatever the tier.

    Always a ``sensor`` / ``binary_sensor``, so this entity never changes
    component and its Home Assistant id is stable for the life of the install.
    The editable twin is ``discovery_for_control``.
    """
    component = writable_component(
        is_bool=entity.register.encoding is Encoding.BOOL32, writable=False
    )
    payload = _base_payload(
        bms_name,
        component=component,
        object_id=entity.object_id,
        name=entity.description,
        topic_suffix=entity.topic_suffix,
        entity_category=_read_only_category(entity.entity_category),
    )
    if entity.register.unit:
        payload["unit_of_measurement"] = entity.register.unit
        # Match the precision the BMS encoding stores — see jk_settings.Encoding.
        decimals = _decimals_for_encoding(entity.register.encoding)
        if decimals is not None:
            payload["suggested_display_precision"] = decimals
    if component is Component.BINARY_SENSOR:
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, component, bms_name, entity.object_id),
        payload=payload,
    )


def _gate_on_tier(payload: dict[str, Any], tier: WriteTier) -> None:
    """Make a control operable only while the bridge is up *and* its tier is on.

    HA disables a control whose entity is unavailable, so gating availability
    keeps the entity permanently registered — stable id, never deleted, never
    re-created — while making it impossible to operate when the write would be
    refused anyway. ``availability`` (a list) must replace ``availability_topic``:
    the two cannot be used together.
    """
    payload.pop("availability_topic", None)
    payload["availability"] = [
        {"topic": BRIDGE_AVAILABILITY_TOPIC},
        {"topic": tier_topic(tier)},
    ]
    payload["availability_mode"] = "all"


def discovery_for_control(
    entity: WritableEntity, bms_name: str, *, discovery_prefix: str
) -> DiscoveryMessage:
    """The editable twin of a setting: a ``number`` / ``switch``.

    Published only while the parameter's write tier is on; when the tier goes
    off this config is cleared and the entity disappears, leaving the
    read-only twin — and its id — untouched.

    Two entities rather than one changing component, because MQTT discovery
    cannot express a read-only ``number`` or ``switch``: ``command_topic`` is
    required for both (``MQTT_RW_SCHEMA``), and HA has no per-entity read-only
    flag. Flipping the component instead would change the entity id on every
    tier toggle, which silently breaks dashboards and automations — issue #23.
    """
    is_bool = entity.register.encoding is Encoding.BOOL32
    component = writable_component(is_bool=is_bool, writable=True)
    object_id = control_object_id(entity.object_id)
    payload = _base_payload(
        bms_name,
        component=component,
        object_id=object_id,
        name=control_name(entity.description),
        topic_suffix=entity.topic_suffix,
        entity_category=entity.entity_category,
    )
    payload["command_topic"] = _command_topic(bms_name, entity.topic_suffix)
    _gate_on_tier(payload, entity.register.tier)
    if entity.register.unit:
        payload["unit_of_measurement"] = entity.register.unit
        decimals = _decimals_for_encoding(entity.register.encoding)
        if decimals is not None:
            payload["suggested_display_precision"] = decimals
    if component is Component.NUMBER:
        payload["min"] = entity.register.min_value
        payload["max"] = entity.register.max_value
        payload["step"] = entity.register.step
        payload["mode"] = "box"
    else:
        payload["payload_on"] = "ON"
        payload["payload_off"] = "OFF"
        payload["state_on"] = "ON"
        payload["state_off"] = "OFF"
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, component, bms_name, object_id),
        payload=payload,
    )


def discovery_for_packed_bit(
    entity: PackedBitEntity, bms_name: str, *, discovery_prefix: str
) -> DiscoveryMessage:
    """Read-only view of a packed-bit toggle — always a ``binary_sensor``."""
    payload = _base_payload(
        bms_name,
        component=Component.BINARY_SENSOR,
        object_id=entity.object_id,
        name=entity.bit.description,
        topic_suffix=entity.topic_suffix,
        entity_category=_read_only_category(entity.entity_category),
    )
    payload["payload_on"] = "ON"
    payload["payload_off"] = "OFF"
    return DiscoveryMessage(
        topic=_discovery_topic(
            discovery_prefix, Component.BINARY_SENSOR, bms_name, entity.object_id
        ),
        payload=payload,
    )


def discovery_for_packed_bit_control(
    entity: PackedBitEntity, bms_name: str, *, discovery_prefix: str
) -> DiscoveryMessage:
    """The editable twin of a packed-bit toggle: a ``switch``."""
    object_id = control_object_id(entity.object_id)
    payload = _base_payload(
        bms_name,
        component=Component.SWITCH,
        object_id=object_id,
        name=control_name(entity.bit.description),
        topic_suffix=entity.topic_suffix,
        entity_category=entity.entity_category,
    )
    payload["command_topic"] = _command_topic(bms_name, entity.topic_suffix)
    _gate_on_tier(payload, entity.bit.tier)
    payload["payload_on"] = "ON"
    payload["payload_off"] = "OFF"
    payload["state_on"] = "ON"
    payload["state_off"] = "OFF"
    return DiscoveryMessage(
        topic=_discovery_topic(discovery_prefix, Component.SWITCH, bms_name, object_id),
        payload=payload,
    )


def discovery_for_bridge_tier(
    sensor: BridgeTierSensor, *, discovery_prefix: str
) -> DiscoveryMessage:
    """Discovery for one write-tier sensor on the bridge's own device."""
    unique_id = f"{BRIDGE_DEVICE_ID}_{sensor.object_id}"
    payload: dict[str, Any] = {
        "name": sensor.name,
        "state_topic": tier_topic(sensor.tier),
        "unique_id": unique_id,
        "device": _bridge_device_info(),
        "availability_topic": BRIDGE_AVAILABILITY_TOPIC,
        "entity_category": "diagnostic",
        "payload_on": "online",
        "payload_off": "offline",
    }
    return DiscoveryMessage(
        topic=(
            f"{discovery_prefix}/{Component.BINARY_SENSOR.value}/"
            f"{unique_id}/config"
        ),
        payload=payload,
    )


def bridge_discovery_messages(*, discovery_prefix: str) -> list[DiscoveryMessage]:
    """Discovery for the bridge device's entities — published once, not per pack."""
    return [
        discovery_for_bridge_tier(s, discovery_prefix=discovery_prefix)
        for s in BRIDGE_TIER_SENSORS
    ]


def tier_state_messages(settings: Settings) -> list[tuple[str, str]]:
    """``(topic, payload)`` for each write tier: ``online`` when on, else ``offline``.

    Retained, so the tier sensors and every control's availability survive a
    Home Assistant restart.
    """
    return [
        (tier_topic(s.tier), "online" if _tier_enabled(settings, s.tier) else "offline")
        for s in BRIDGE_TIER_SENSORS
    ]


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

    Writable entities are published as controls only when the matching tier
    toggle is on. Entities flagged ``verified=False`` are skipped unless
    ``settings.debug_unverified_fields`` is True.

    Alongside the configs, removals (``payload=None``) are emitted for every
    topic a previous run with other settings could have left retained: the
    other component of each writable / packed bit, cells above ``cell_count``
    and hidden unverified entities.
    """
    discovery_prefix = settings.discovery_prefix
    debug = settings.debug_unverified_fields
    messages: list[DiscoveryMessage] = []

    def remove(component: Component, object_id: str) -> None:
        messages.append(discovery_removal(discovery_prefix, component, bms_name, object_id))

    for e in (*LIVE_SENSORS, *LIVE_BINARY_SENSORS):
        if not e.verified and not debug:
            remove(e.component, e.object_id)
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
    # The runner announces with a default of MAX_CELLS before the first poll,
    # so a smaller pack would otherwise keep those extra cells retained.
    for n in range(cell_count + 1, MAX_CELLS + 1):
        remove(Component.SENSOR, f"cell_{n}_volt")
        remove(Component.SENSOR, f"cell_{n}_ohm")
    for e in FIXED_SENSORS:
        if not e.verified and not debug:  # pragma: no branch - no unverified FIXED entries today
            continue  # pragma: no cover
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))
    for e in BRIDGE_SENSORS:
        messages.append(discovery_for_read_only(e, bms_name, discovery_prefix=discovery_prefix))

    for w in WRITABLE_ENTITIES:
        control = writable_component(
            is_bool=w.register.encoding is Encoding.BOOL32, writable=True
        )
        # Before 2.4 the control was published under the setting's own
        # object_id, and the read-only view took that id when the tier was off.
        # Clear that topic always, so the old entity cannot linger.
        remove(control, w.object_id)
        if not w.verified and not debug:  # pragma: no branch - no unverified writables today
            remove(Component.SENSOR, w.object_id)  # pragma: no cover
            remove(control, control_object_id(w.object_id))  # pragma: no cover
            continue  # pragma: no cover
        messages.append(discovery_for_writable(w, bms_name, discovery_prefix=discovery_prefix))
        # Published whatever the tier: the control's availability is gated
        # instead, so the entity is never deleted and never re-registered.
        messages.append(discovery_for_control(w, bms_name, discovery_prefix=discovery_prefix))

    for p in PACKED_BIT_ENTITIES:
        remove(Component.SWITCH, p.object_id)
        if not p.verified and not debug:
            remove(Component.BINARY_SENSOR, p.object_id)
            remove(Component.SWITCH, control_object_id(p.object_id))
            continue
        messages.append(discovery_for_packed_bit(p, bms_name, discovery_prefix=discovery_prefix))
        messages.append(
            discovery_for_packed_bit_control(p, bms_name, discovery_prefix=discovery_prefix)
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


def state_message_last_seen(bms_name: str, when: datetime) -> tuple[str, str]:
    """``(topic, payload)`` for the ``last_seen`` timestamp sensor.

    HA's ``timestamp`` device class needs an ISO 8601 string with a timezone,
    so a naive datetime is rejected rather than silently published.
    """
    if when.tzinfo is None:
        raise ValueError("last_seen timestamp must be timezone-aware")
    (entity,) = BRIDGE_SENSORS
    return _state_topic(bms_name, entity.topic_suffix), when.isoformat(timespec="seconds")


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
    """Serialise a discovery message for ``mqtt.publish``; a removal is ``b""``."""
    if message.payload is None:
        return message.topic, b""
    return message.topic, json.dumps(message.payload, separators=(",", ":")).encode()
