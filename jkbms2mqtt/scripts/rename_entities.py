#!/usr/bin/env python3
"""Rename existing Home Assistant entity ids to the ids this bridge's names produce.

Home Assistant never renames an entity that is already in its registry, so an
install that ran an older jkbms2mqtt keeps whatever ids it registered back then
(for example ``sensor.bms_1_total_pack_voltage`` or, from the builds that sent
``default_entity_id``, ``sensor.bms_1_device_total_voltage``). Fresh installs
get ``sensor.bms_1_total_voltage``: HA slugifies the device name plus the
entity name.

This script closes that gap. It reads the entity registry over the websocket
API, matches every entity by the bridge's own ``unique_id``
(``<bms_name>_device_<object_id>``) — not by its current id — and renames it to
the id the current entity table would produce.

Usage:
    export HA_URL="http://homeassistant.local:8123"
    export HA_TOKEN="<long-lived access token>"   # Profile -> Security
    python scripts/rename_entities.py             # dry run: prints the plan
    python scripts/rename_entities.py --apply     # performs the renames

Notes:
- Dry run by default. Nothing changes without ``--apply``.
- Entities whose id already matches are left alone.
- A rename does NOT update automations, scripts, scenes, template sensors or
  hand-written dashboards; they keep referencing the old ids. The add-on's
  generated dashboard follows this scheme automatically.
- Home Assistant moves an entity's recorded history and statistics along with
  the rename.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import unicodedata

from jkbms2mqtt.entities import (
    BRIDGE_SENSORS,
    CELL_STATS_SENSORS,
    FIXED_SENSORS,
    LIVE_BINARY_SENSORS,
    LIVE_SENSORS,
    PACKED_BIT_ENTITIES,
    WRITABLE_ENTITIES,
    expand_cell_entities,
)
from jkbms2mqtt.mqtt import writable_component
from jkbms2mqtt.protocol.jk_modbus import MAX_CELLS
from jkbms2mqtt.protocol.jk_settings import Encoding


def _connect(url: str):
    """Open the websocket connection, reporting a missing dependency clearly.

    Imported lazily so ``plan()`` and ``target_object_ids()`` stay importable
    (and testable) without the ``websockets`` package installed.
    """
    try:
        import websockets
    except ImportError:  # pragma: no cover - operator tooling
        sys.exit("This script needs the 'websockets' package: pip install websockets")
    return websockets.connect(url, max_size=None)


def ha_slugify(text: str) -> str:
    """Slugify the way Home Assistant builds an entity id from a name."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def target_object_ids() -> dict[str, tuple[str, str]]:
    """Map ``object_id -> (domain, entity-name slug)`` for every entity.

    Writable settings are listed in their read-only form; a tier that is on
    publishes them as ``number`` / ``switch`` instead, which this script
    resolves per entity from the registry's own domain.
    """
    out: dict[str, tuple[str, str]] = {}
    for e in (
        *LIVE_SENSORS,
        *LIVE_BINARY_SENSORS,
        *CELL_STATS_SENSORS,
        *FIXED_SENSORS,
        *BRIDGE_SENSORS,
        *expand_cell_entities(MAX_CELLS),
    ):
        out[e.object_id] = (e.component.value, ha_slugify(e.description.rstrip(".")))
    for w in WRITABLE_ENTITIES:
        component = writable_component(
            is_bool=w.register.encoding is Encoding.BOOL32, writable=False
        )
        out[w.object_id] = (component.value, ha_slugify(w.description.rstrip(".")))
    for p in PACKED_BIT_ENTITIES:
        out[p.object_id] = ("binary_sensor", ha_slugify(p.bit.description.rstrip(".")))
    return out


_UNIQUE_ID = re.compile(r"^(?P<bms>.+)_device_(?P<object_id>.+)$")


def plan(entries: list[dict], targets: dict[str, tuple[str, str]]) -> list[tuple[str, str]]:
    """Return ``(old_entity_id, new_entity_id)`` for every entity that must move."""
    taken = {e["entity_id"] for e in entries}
    rows: list[tuple[str, str]] = []
    for entry in entries:
        unique_id = entry.get("unique_id") or ""
        match = _UNIQUE_ID.match(unique_id)
        if not match or match["object_id"] not in targets:
            continue
        domain = entry["entity_id"].split(".", 1)[0]
        _, slug = targets[match["object_id"]]
        new_id = f"{domain}.{ha_slugify(match['bms'])}_{slug}"
        old_id = entry["entity_id"]
        if new_id == old_id:
            continue
        if new_id in taken:
            print(f"  SKIP {old_id} -> {new_id} (id already in use)")
            continue
        taken.add(new_id)
        rows.append((old_id, new_id))
    return rows


async def run(apply: bool) -> int:
    url = os.environ["HA_URL"].rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    token = os.environ["HA_TOKEN"]

    async with _connect(url) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            sys.exit(f"authentication failed: {auth}")

        next_id = 1

        async def call(payload: dict) -> dict:
            nonlocal next_id
            payload["id"] = next_id
            next_id += 1
            await ws.send(json.dumps(payload))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == payload["id"] and reply.get("type") == "result":
                    return reply

        registry = await call({"type": "config/entity_registry/list"})
        rows = plan(registry["result"], target_object_ids())

        if not rows:
            print("Nothing to rename: every entity already uses the current naming.")
            return 0
        print(f"{len(rows)} entities to rename")
        if not apply:
            for old, new in rows:
                print(f"  {old} -> {new}")
            print("\nRe-run with --apply to perform these renames.")
            return 0

        failed = 0
        for old, new in rows:
            reply = await call(
                {
                    "type": "config/entity_registry/update",
                    "entity_id": old,
                    "new_entity_id": new,
                }
            )
            if reply.get("success"):
                print(f"  OK   {old} -> {new}")
            else:
                failed += 1
                print(f"  FAIL {old} -> {new}: {reply.get('error')}")
        print(f"\n{len(rows) - failed} renamed, {failed} failed")
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the renames")
    args = parser.parse_args()
    for var in ("HA_URL", "HA_TOKEN"):
        if var not in os.environ:
            sys.exit(f"{var} is not set; see the docstring for usage")
    return asyncio.run(run(args.apply))


if __name__ == "__main__":  # pragma: no cover - operator tooling
    raise SystemExit(main())
