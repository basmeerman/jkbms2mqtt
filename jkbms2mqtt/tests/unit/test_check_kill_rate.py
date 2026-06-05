"""Tests for scripts/check_kill_rate.py — the CI mutmut 3.x gate.

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
from check_kill_rate import Counts, kill_rate, parse_results  # noqa: E402


def _results(*lines: str) -> str:
    """Build a `mutmut results --all true`-shaped blob (indented like the tool)."""
    return "".join(f"    {line}\n" for line in lines)


class TestParseResults:
    def test_buckets_by_select(self) -> None:
        text = _results(
            "jkbms2mqtt.protocol.jk_modbus.x_a__mutmut_1: killed",
            "jkbms2mqtt.protocol.jk_modbus.x_a__mutmut_2: survived",
            "jkbms2mqtt.entities.x_b__mutmut_1: killed",
        )
        counts, missed = parse_results(text, select=".protocol.")
        assert counts == Counts(caught=1, missed=1, ignored=0)
        assert missed == ["jkbms2mqtt.protocol.jk_modbus.x_a__mutmut_2"]

    def test_entities_select(self) -> None:
        text = _results(
            "jkbms2mqtt.protocol.jk_modbus.x_a__mutmut_1: survived",
            "jkbms2mqtt.entities.x_b__mutmut_1: killed",
        )
        counts, _ = parse_results(text, select=".entities.")
        assert counts == Counts(caught=1, missed=0, ignored=0)

    def test_caught_statuses(self) -> None:
        text = _results(
            "m.protocol.a__mutmut_1: killed",
            "m.protocol.a__mutmut_2: timeout",
            "m.protocol.a__mutmut_3: caught by type check",
        )
        counts, missed = parse_results(text, select=".protocol.")
        assert counts == Counts(caught=3, missed=0, ignored=0)
        assert missed == []

    def test_missed_statuses(self) -> None:
        text = _results(
            "m.protocol.a__mutmut_1: survived",
            "m.protocol.a__mutmut_2: no tests",
            "m.protocol.a__mutmut_3: suspicious",
            "m.protocol.a__mutmut_4: segfault",
        )
        counts, missed = parse_results(text, select=".protocol.")
        assert counts == Counts(caught=0, missed=4, ignored=0)
        assert len(missed) == 4

    def test_skipped_is_ignored(self) -> None:
        text = _results(
            "m.protocol.a__mutmut_1: killed",
            "m.protocol.a__mutmut_2: skipped",
        )
        counts, _ = parse_results(text, select=".protocol.")
        assert counts == Counts(caught=1, missed=0, ignored=1)
        assert counts.total == 1  # ignored excluded from the denominator

    def test_unknown_status_counts_as_missed(self) -> None:
        text = _results("m.protocol.a__mutmut_1: teleported")
        counts, missed = parse_results(text, select=".protocol.")
        assert counts == Counts(caught=0, missed=1, ignored=0)
        assert missed == ["m.protocol.a__mutmut_1 (teleported)"]

    def test_incomplete_status_raises(self) -> None:
        text = _results("m.protocol.a__mutmut_1: not checked")
        with pytest.raises(ValueError, match="did not finish"):
            parse_results(text, select=".protocol.")

    def test_interrupted_status_raises(self) -> None:
        text = _results("m.protocol.a__mutmut_1: check was interrupted by user")
        with pytest.raises(ValueError, match="did not finish"):
            parse_results(text, select=".protocol.")

    def test_blank_and_unparseable_lines_skipped(self) -> None:
        text = "\n   \nheader without colon separator\n" + _results(
            "m.protocol.a__mutmut_1: killed"
        )
        counts, _ = parse_results(text, select=".protocol.")
        assert counts == Counts(caught=1, missed=0, ignored=0)

    def test_status_with_colon_in_name(self) -> None:
        # rpartition keeps the last ": " as the status separator.
        text = _results("m.protocol.weird::name__mutmut_1: killed")
        counts, _ = parse_results(text, select=".protocol.")
        assert counts == Counts(caught=1, missed=0, ignored=0)


class TestKillRate:
    def test_perfect(self) -> None:
        assert kill_rate(Counts(caught=10, missed=0, ignored=0)) == 1.0

    def test_zero(self) -> None:
        assert kill_rate(Counts(caught=0, missed=10, ignored=0)) == 0.0

    def test_partial(self) -> None:
        assert kill_rate(Counts(caught=8, missed=2, ignored=0)) == pytest.approx(0.8)

    def test_empty_total_returns_one(self) -> None:
        """Avoid ZeroDivisionError on an empty mutmut run."""
        assert kill_rate(Counts(caught=0, missed=0, ignored=0)) == 1.0


class TestCli:
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def _file(self, tmp_path: Path, *lines: str) -> str:
        p = tmp_path / "results.txt"
        p.write_text(_results(*lines))
        return str(p)

    def test_pass_above_threshold(self, tmp_path: Path) -> None:
        f = self._file(
            tmp_path,
            *[f"m.protocol.a__mutmut_{i}: killed" for i in range(9)],
            "m.protocol.a__mutmut_9: survived",
        )
        result = self._run(f, "--min", "0.8", "--select", ".protocol.")
        assert result.returncode == 0
        assert "kill_rate=90.0%" in result.stdout

    def test_fail_below_threshold(self, tmp_path: Path) -> None:
        f = self._file(
            tmp_path,
            *[f"m.protocol.a__mutmut_{i}: killed" for i in range(5)],
            *[f"m.protocol.a__mutmut_{5 + i}: survived" for i in range(5)],
        )
        result = self._run(f, "--min", "0.8", "--select", ".protocol.")
        assert result.returncode == 1
        assert "FAIL" in result.stderr

    def test_exact_threshold_passes(self, tmp_path: Path) -> None:
        f = self._file(
            tmp_path,
            *[f"m.protocol.a__mutmut_{i}: killed" for i in range(8)],
            *[f"m.protocol.a__mutmut_{8 + i}: survived" for i in range(2)],
        )
        result = self._run(f, "--min", "0.8", "--select", ".protocol.")
        assert result.returncode == 0

    def test_label_appears_in_output(self, tmp_path: Path) -> None:
        f = self._file(tmp_path, "m.protocol.a__mutmut_1: killed")
        result = self._run(
            f, "--min", "0.9", "--select", ".protocol.", "--label", "protocol"
        )
        assert "protocol mutants" in result.stdout

    def test_show_survivors_lists_them(self, tmp_path: Path) -> None:
        f = self._file(
            tmp_path,
            "m.protocol.a__mutmut_1: killed",
            "m.protocol.a__mutmut_2: survived",
        )
        result = self._run(
            f, "--min", "0.1", "--select", ".protocol.", "--show-survivors"
        )
        assert result.returncode == 0
        assert "survivors (1)" in result.stdout
        assert "m.protocol.a__mutmut_2" in result.stdout

    def test_show_survivors_noop_when_none(self, tmp_path: Path) -> None:
        f = self._file(tmp_path, "m.protocol.a__mutmut_1: killed")
        result = self._run(
            f, "--min", "0.1", "--select", ".protocol.", "--show-survivors"
        )
        assert "survivors" not in result.stdout

    def test_no_match_is_error(self, tmp_path: Path) -> None:
        f = self._file(tmp_path, "m.entities.a__mutmut_1: killed")
        result = self._run(f, "--min", "0.8", "--select", ".protocol.")
        assert result.returncode == 2
        assert "no mutants matched" in result.stderr

    def test_incomplete_run_is_error(self, tmp_path: Path) -> None:
        f = self._file(tmp_path, "m.protocol.a__mutmut_1: not checked")
        result = self._run(f, "--min", "0.8", "--select", ".protocol.")
        assert result.returncode == 2
        assert "did not finish" in result.stderr

    def test_invalid_min_rejected_low(self, tmp_path: Path) -> None:
        f = self._file(tmp_path, "m.protocol.a__mutmut_1: killed")
        result = self._run(f, "--min", "-0.1", "--select", ".protocol.")
        assert result.returncode == 2
        assert "must be in [0, 1]" in result.stderr

    def test_invalid_min_rejected_high(self, tmp_path: Path) -> None:
        f = self._file(tmp_path, "m.protocol.a__mutmut_1: killed")
        result = self._run(f, "--min", "1.5", "--select", ".protocol.")
        assert result.returncode == 2
