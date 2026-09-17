#!/usr/bin/env python3
"""CI guard: the dashboard must reference exactly the bridge's published entities.

The bridge's entity table (``jkbms2mqtt.entities``) is the source of truth for
what gets published. This script enumerates that table, then enumerates every
entity the generated dashboard + aggregates package reference, reconciles the
two at the ``(domain, object_id)`` level, and fails the build on any drift:

- a verified bridge entity the dashboard does NOT surface (coverage gap), or
- a dashboard reference with no matching bridge entity (stale / typo'd ref).

So if someone adds, removes, or renames an entity in ``entities.py`` /
``jk_settings.py`` without updating the dashboard, the build goes red.

Every setting is published twice — a read-only ``sensor`` / ``binary_sensor``
twin and a ``number`` / ``switch`` control — and both exist whatever the write
tiers are set to: a tier gates the control's *availability*, not its existence.
The dashboard shows both, one per conditional row, so the check is tier-free.

Entity ids come from the entity names via Home Assistant's slug rule, so a
description edit changes an id. This check works on object ids and therefore
catches *set* drift, not slug drift; for that, run
``out/verify-entities.jinja`` against a live instance.

Exit code 0 = in sync, 1 = drift (prints the offending entities).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from jkbms2mqtt import dashboard as generate  # slug table + card builders (the real generator)
from jkbms2mqtt.entities import (
    BRIDGE_SENSORS,
    CELL_STATS_SENSORS,
    FIXED_SENSORS,
    LIVE_BINARY_SENSORS,
    LIVE_SENSORS,
    WRITABLE_ENTITIES,
    control_object_id,
    expand_cell_entities,
    writable_component,
)
from jkbms2mqtt.protocol.jk_settings import Encoding

HERE = Path(__file__).parent
# Cell count the committed sample is generated with (see the header of the YAML).
CELLS = 16
# Verified bridge entities intentionally not shown on the dashboard, if any.
# Empty today — every verified entity is surfaced. Add "(domain, object_id)"
# tuples here (with a reason) to consciously exclude one.
ALLOW_MISSING: set[tuple[str, str]] = set()

_REF = re.compile(r"\b(sensor|binary_sensor|number|switch)\.bms_1_([a-z0-9_]+)")
# entity-id slug -> object_id, the reverse of the generator's slug table.
_BY_SLUG = {slug: object_id for object_id, slug in generate.SLUG.items()}


def bridge_entities() -> set[tuple[str, str]]:
    """The (domain, object_id) set the bridge publishes (verified only).

    Unverified entities (heating / heating_current / packed bits) are hidden by
    default on both sides, so they are excluded here too.
    """
    out: set[tuple[str, str]] = set()
    read_only = (
        *LIVE_SENSORS,
        *LIVE_BINARY_SENSORS,
        *CELL_STATS_SENSORS,
        *FIXED_SENSORS,
        *BRIDGE_SENSORS,
        *expand_cell_entities(CELLS),
    )
    for e in read_only:
        if e.verified:
            out.add((e.component.value, e.object_id))
    for w in WRITABLE_ENTITIES:
        if w.verified:
            is_bool = w.register.encoding is Encoding.BOOL32
            # Both twins exist whatever the tier; the tier gates availability.
            out.add((writable_component(is_bool=is_bool, writable=False).value, w.object_id))
            out.add(
                (
                    writable_component(is_bool=is_bool, writable=True).value,
                    control_object_id(w.object_id),
                )
            )
    return out


def dashboard_entities() -> set[tuple[str, str]]:
    """Every (domain, object_id) the dashboard + package reference.

    Scans BMS_1 references; the bank aggregates (``*.jkbms_*``) don't match the
    ``bms_1_`` prefix and are correctly ignored.
    """
    texts = [
        generate.dump_yaml(generate.build_dashboard([1], {1: CELLS})),
        generate.dump_yaml(generate.aggregates_package([1])),
    ]
    out: set[tuple[str, str]] = set()
    for text in texts:
        for domain, slug in _REF.findall(text):
            out.add((domain, _BY_SLUG.get(slug, slug)))
    return out


def _check() -> bool:
    bridge = bridge_entities()
    dash = dashboard_entities()

    missing = sorted(bridge - dash - ALLOW_MISSING)  # bridge has, dashboard lacks
    unknown = sorted(dash - bridge)  # dashboard refs, bridge doesn't publish

    if not missing and not unknown:
        print(f"OK: dashboard references all {len(bridge)} verified entities, no extras.")
        return True

    print("DRIFT:")
    for domain, oid in missing:
        print(f"    + {domain}.<bms>_{oid} — published, not on the dashboard")
    for domain, oid in unknown:
        print(f"    - {domain}.<bms>_{oid} — on the dashboard, not published")
    return False


def main() -> int:
    if _check():
        return 0
    print(
        "\nFix: update the generator (card builders in jkbms2mqtt/dashboard.py) to match "
        "the bridge's entity table, regenerate, and commit. If an omission is "
        "intentional, add it to ALLOW_MISSING with a reason."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
