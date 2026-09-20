"""Tests for the write ledger — the ordering guard behind issue #34.

The ledger decides whether a settings capture is still worth publishing. Get it
wrong in one direction and a successful write appears to fail; wrong in the
other and a genuinely failed write is hidden from the user.
"""

from __future__ import annotations

from jkbms2mqtt.write_ledger import WriteLedger


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class TestWriteLedger:
    def test_unknown_parameter_is_never_superseded(self) -> None:
        led = WriteLedger()
        assert led.written_since("BMS_1", "never_written", 0.0) is False

    def test_capture_before_the_write_is_superseded(self) -> None:
        clk = _Clock()
        led = WriteLedger(clock=clk)
        captured = clk.t
        clk.t += 1.0
        led.mark("BMS_1", "charging_switch")
        assert led.written_since("BMS_1", "charging_switch", captured) is True

    def test_capture_after_the_write_is_kept(self) -> None:
        clk = _Clock()
        led = WriteLedger(clock=clk)
        led.mark("BMS_1", "charging_switch")
        clk.t += 1.0
        assert led.written_since("BMS_1", "charging_switch", clk.t) is False

    def test_simultaneous_capture_and_write_counts_as_superseded(self) -> None:
        """A tie is unorderable; preferring the write is the safe side.

        The echo already carries the new value and the next poll republishes
        the truth either way, so dropping the capture costs nothing.
        """
        clk = _Clock()
        led = WriteLedger(clock=clk)
        led.mark("BMS_1", "charging_switch")
        assert led.written_since("BMS_1", "charging_switch", clk.t) is True

    def test_packs_are_independent(self) -> None:
        clk = _Clock()
        led = WriteLedger(clock=clk)
        captured = clk.t
        clk.t += 1.0
        led.mark("BMS_1", "charging_switch")
        assert led.written_since("BMS_1", "charging_switch", captured) is True
        assert led.written_since("BMS_2", "charging_switch", captured) is False

    def test_parameters_are_independent(self) -> None:
        """A block-level guard would wrongly suppress untouched parameters."""
        clk = _Clock()
        led = WriteLedger(clock=clk)
        captured = clk.t
        clk.t += 1.0
        led.mark("BMS_1", "charging_switch")
        assert led.written_since("BMS_1", "max_charge_current", captured) is False

    def test_latest_write_wins(self) -> None:
        clk = _Clock()
        led = WriteLedger(clock=clk)
        led.mark("BMS_1", "charging_switch")
        captured = clk.t + 1.0
        clk.t += 2.0
        led.mark("BMS_1", "charging_switch")
        assert led.written_since("BMS_1", "charging_switch", captured) is True

    def test_default_clock_is_monotonic(self) -> None:
        led = WriteLedger()
        led.mark("BMS_1", "x")
        assert led.written_since("BMS_1", "x", 0.0) is True
