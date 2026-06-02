"""Tests for scripts/check_kill_rate.py — the CI mutmut gate.

If this gate misreports, the mutation-testing CI is silently broken. Every
branch must be covered.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_kill_rate.py"

# Make the script importable as a module under test as well.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from check_kill_rate import Counts, count_results, kill_rate  # noqa: E402


def _write_junit(path: Path, killed: int, survived: int) -> Path:
    """Synthesize a minimal mutmut-shaped JUnit XML file."""
    cases: list[str] = []
    for i in range(killed):
        cases.append(f'<testcase name="killed_{i}" classname="mutmut"/>')
    for i in range(survived):
        cases.append(
            f'<testcase name="survived_{i}" classname="mutmut">'
            f'<failure message="mutant survived">trace</failure>'
            f"</testcase>"
        )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuites><testsuite name="mutmut">{"".join(cases)}'
        "</testsuite></testsuites>"
    )
    path.write_text(xml)
    return path


class TestCountResults:
    def test_only_killed(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=10, survived=0)
        counts = count_results(xml)
        assert counts == Counts(total=10, killed=10, survived=0)

    def test_only_survived(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=0, survived=5)
        counts = count_results(xml)
        assert counts == Counts(total=5, killed=0, survived=5)

    def test_mixed(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=8, survived=2)
        counts = count_results(xml)
        assert counts == Counts(total=10, killed=8, survived=2)

    def test_empty(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=0, survived=0)
        counts = count_results(xml)
        assert counts == Counts(total=0, killed=0, survived=0)


class TestKillRate:
    def test_perfect(self) -> None:
        assert kill_rate(Counts(total=10, killed=10, survived=0)) == 1.0

    def test_zero(self) -> None:
        assert kill_rate(Counts(total=10, killed=0, survived=10)) == 0.0

    def test_partial(self) -> None:
        assert kill_rate(Counts(total=10, killed=8, survived=2)) == pytest.approx(0.8)

    def test_empty_total_returns_one(self) -> None:
        """Avoid ZeroDivisionError on an empty mutmut run."""
        assert kill_rate(Counts(total=0, killed=0, survived=0)) == 1.0


class TestCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_pass_above_threshold(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=9, survived=1)
        result = self._run(str(xml), "--min", "0.8")
        assert result.returncode == 0
        assert "kill_rate=90.0%" in result.stdout

    def test_fail_below_threshold(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=5, survived=5)
        result = self._run(str(xml), "--min", "0.8")
        assert result.returncode == 1
        assert "FAIL" in result.stderr

    def test_exact_threshold_passes(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=8, survived=2)
        result = self._run(str(xml), "--min", "0.8")
        assert result.returncode == 0

    def test_label_appears_in_output(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=10, survived=0)
        result = self._run(str(xml), "--min", "0.9", "--label", "protocol")
        assert "protocol mutants" in result.stdout

    def test_invalid_min_rejected_low(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=10, survived=0)
        result = self._run(str(xml), "--min", "-0.1")
        assert result.returncode == 2
        assert "must be in [0, 1]" in result.stderr

    def test_invalid_min_rejected_high(self, tmp_path: Path) -> None:
        xml = _write_junit(tmp_path / "x.xml", killed=10, survived=0)
        result = self._run(str(xml), "--min", "1.5")
        assert result.returncode == 2
