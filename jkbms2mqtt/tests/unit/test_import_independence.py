"""The operator tooling must import without the bridge's runtime dependencies.

`scripts/rename_entities.py` runs on an operator's machine against a live Home
Assistant, and the dashboard generator runs in CI and in the add-on. Neither
needs pydantic, aiomqtt or pymodbus — only the entity table, which is
standard-library only. Issue #24: they used to pull in pydantic through
`jkbms2mqtt.mqtt` -> `jkbms2mqtt.config`, so migrating an install meant
building a virtualenv with the whole package.

Each check runs in a subprocess with the modules blocked at import time, so an
accidental import anywhere in the chain fails loudly instead of being masked by
a module another test already imported.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
SCRIPTS = REPO_ROOT / "scripts"

_BLOCKER = """
import sys
from importlib.abc import MetaPathFinder


class Blocker(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {blocked!r}:
            raise ImportError("BLOCKED: " + fullname)
        return None


sys.meta_path.insert(0, Blocker())
{body}
assert "pydantic" not in sys.modules, "pydantic was imported"
print("OK")
"""


def _run(*, blocked: set[str], body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _BLOCKER.format(blocked=blocked, body=body)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": f"{SRC}:{SCRIPTS}", "PATH": "/usr/bin:/bin"},
        check=False,
    )


def test_rename_script_imports_without_bridge_dependencies() -> None:
    """The migration tool needs only `websockets` plus the standard library."""
    result = _run(
        blocked={"pydantic", "yaml", "aiomqtt", "pymodbus"},
        body=(
            "import rename_entities as r\n"
            "targets = r.target_object_ids()\n"
            "assert targets['total_voltage'] == ('sensor', 'total_voltage'), targets['total_voltage']\n"
            "assert targets['charging_switch'] == ('binary_sensor', 'charging')\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_dashboard_generator_imports_without_pydantic() -> None:
    """The generator needs PyYAML, but not the bridge's config/MQTT layer."""
    result = _run(
        blocked={"pydantic", "aiomqtt", "pymodbus"},
        body=(
            "from jkbms2mqtt import dashboard\n"
            "assert dashboard.build_dashboard([1], {1: 16})['views']\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_blocker_actually_blocks() -> None:
    """Control: the bridge itself still needs pydantic.

    Without this, the two checks above would pass just as happily if the
    blocker silently stopped working.
    """
    result = _run(blocked={"pydantic"}, body="import jkbms2mqtt.mqtt\n")
    assert result.returncode != 0
    assert "BLOCKED: pydantic" in result.stderr
