#!/usr/bin/env python3
"""Thin CLI wrapper around ``jkbms2mqtt.dashboard`` (the real generator).

The generator lives in the add-on package so the add-on can import it to
auto-install the dashboard on startup. This shim keeps the familiar repo
entry point and writes into ``dashboards/out`` + ``dashboards/packages``.

    python dashboards/generate.py --bms-ids 1,2,3,4,5,6 --cells 16
    python dashboards/generate.py --bms-ids 1,3,7 --cells 1=16,3=8,7=24 --naming legacy
"""

from __future__ import annotations

import sys
from pathlib import Path

from jkbms2mqtt.dashboard import main

if __name__ == "__main__":
    sys.exit(main(default_dir=Path(__file__).parent))
