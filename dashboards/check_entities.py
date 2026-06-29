#!/usr/bin/env python3
"""CI guard: the dashboard must reference exactly the bridge's published entities.

The bridge's entity table (``jkbms2mqtt.entities``) is the source of truth for
what gets published. This script enumerates that table, then enumerates every
entity the generated dashboard + aggregates package reference, reconciles the
two through the generator's ``SLUG`` map, and fails the build on any drift:

- a verified bridge entity the dashboard does NOT surface (coverage gap), or
- a dashboard reference with no matching bridge entity (stale / typo'd ref).

So if someone adds, removes, or renames an entity in ``entities.py`` /
``jk_settings.py`` without updating the dashboard, the build goes red.

Reconciliation works at the ``(domain, object_id)`` level, NOT on the rendered
entity-id string — the deployed bridge names entities by a non-uniform
device-name + description rule that is not reproducible from source (verified
empirically; see PLAN.md). That means this check catches *set* drift
(add/remove/rename of an entity) but NOT a description-text edit that only
changes an HA slug. For slug drift, run ``out/verify-entities.jinja`` against a
live instance.

Exit code 0 = in sync, 1 = drift (prints the offending entities).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from jkbms2mqtt import dashboard as generate  # SLUG map + cell rules (the real generator)
from jkbms2mqtt.entities import (
    CELL_STATS_SENSORS,
    FIXED_SENSORS,
    LIVE_BINARY_SENSORS,
    LIVE_SENSORS,
    WRITABLE_ENTITIES,
    expand_cell_entities,
)

HERE = Path(__file__).parent
# Cell count the committed sample is generated with (see the header of the YAML).
CELLS = 16
# Verified bridge entities intentionally not shown on the dashboard, if any.
# Empty today — every verified entity is surfaced. Add "(domain, object_id)"
# tuples here (with a reason) to consciously exclude one.
ALLOW_MISSING: set[tuple[str, str]] = set()

_REF = re.compile(r"\b(sensor|binary_sensor|number|switch)\.bms_1_([a-z0-9_]+)")
_INV_SLUG = {v: k for k, v in generate.SLUG.items()}


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
        *expand_cell_entities(CELLS),
    )
    for e in read_only:
        if e.verified:
            out.add((e.component.value, e.object_id))
    for w in WRITABLE_ENTITIES:
        if w.verified:
            out.add((w.component.value, w.object_id))
    return out


def _slug_to_object_id(slug: str, naming: str) -> str:
    """Reverse the generator's naming: real entity slug -> bridge object_id."""
    if naming == "device":
        return slug.removeprefix("device_")
    if m := re.match(r"^cell_(\d+)_voltage$", slug):
        return f"cell_{m.group(1)}_volt"
    if m := re.match(r"^cell_(\d+)_internal_resistance$", slug):
        return f"cell_{m.group(1)}_ohm"
    return _INV_SLUG.get(slug, slug)


def _dashboard_texts(naming: str) -> list[str]:
    """The dashboard + package YAML to scan for the given naming mode.

    ``legacy`` reads the committed sample (the canonical artifact); ``device``
    builds in-memory (the add-on's auto-install output isn't committed).
    """
    if naming == "legacy":
        return [
            (HERE / "out/jkbms2mqtt-dashboard.yaml").read_text(),
            (HERE / "packages/jkbms_aggregates.yaml").read_text(),
        ]
    generate._set_naming("device")
    return [
        generate.dump_yaml(generate.build_dashboard([1], {1: CELLS})),
        generate.dump_yaml(generate.aggregates_package([1])),
    ]


def dashboard_entities(naming: str) -> set[tuple[str, str]]:
    """Every (domain, object_id) the dashboard + package reference.

    Scans BMS_1 references; the bank aggregates (``*.jkbms_*``) don't match the
    ``bms_1_`` prefix and are correctly ignored.
    """
    out: set[tuple[str, str]] = set()
    for text in _dashboard_texts(naming):
        for domain, slug in _REF.findall(text):
            out.add((domain, _slug_to_object_id(slug, naming)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--naming", choices=["legacy", "device"], default="legacy")
    args = ap.parse_args()

    bridge = bridge_entities()
    dash = dashboard_entities(args.naming)

    missing = sorted(bridge - dash - ALLOW_MISSING)  # bridge has, dashboard lacks
    unknown = sorted(dash - bridge)  # dashboard refs, bridge doesn't publish

    if not missing and not unknown:
        print(
            f"OK ({args.naming}): dashboard references all {len(bridge)} "
            "verified bridge entities, no extras."
        )
        return 0

    if missing:
        print("DRIFT — bridge publishes these, but the dashboard does not reference them:")
        for domain, oid in missing:
            print(f"  + {domain}.<bms>_{oid}")
    if unknown:
        print("DRIFT — dashboard references these, but the bridge does not publish them:")
        for domain, oid in unknown:
            print(f"  - {domain}.<bms>_{oid}")
    print(
        "\nFix: update dashboards/generate.py (SLUG map / card builders) to match "
        "the bridge's entity table, regenerate, and commit. If an omission is "
        "intentional, add it to ALLOW_MISSING with a reason."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
