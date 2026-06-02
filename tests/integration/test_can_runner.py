"""Integration tests for the CAN bus runner."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

import pytest

from jkbms2mqtt.can_runner import CanFrameAccumulator, CanRunner
from jkbms2mqtt.config import Settings
from jkbms2mqtt.protocol.can_protocol import (
    ID_CELL_MINMAX,
    ID_CELL_VOLT_BASE,
    ID_INDIVIDUAL_TEMPS,
    ID_MAIN_STATUS,
    ID_POWER_CURRENT,
)
from jkbms2mqtt.protocol.capabilities import Topology, Transport
from jkbms2mqtt.transport.can_bus import CanMessage


@dataclass
class FakeMqttClient:
    published: list[tuple[str, bytes, int, bool]] = field(default_factory=list)

    async def publish(self, topic, payload, qos=0, retain=False) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        self.published.append((topic, payload, qos, retain))


@dataclass
class FakeCanTransport:
    queue: deque[CanMessage] = field(default_factory=deque)
    _connected: bool = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def aclose(self) -> None:
        self._connected = False

    async def recv_message(self, *, timeout_s: float) -> CanMessage | None:
        await asyncio.sleep(0)
        if not self.queue:
            await asyncio.sleep(0.01)  # simulate brief poll
            return None
        return self.queue.popleft()


def _settings_can() -> Settings:
    return Settings(
        transport=Transport.CAN_BUS,
        topology=Topology.CAN,
    )


def _main_status_bytes(voltage_v: float = 53.0, soc: int = 50, current_a: float = 0.0) -> bytes:
    voltage_raw = int(round(voltage_v / 0.1))
    current_raw = int(round((current_a + 400) / 0.1))
    return bytes(
        [
            voltage_raw & 0xFF, (voltage_raw >> 8) & 0xFF,
            current_raw & 0xFF, (current_raw >> 8) & 0xFF,
            soc,
            0, 0, 0,
        ]
    )


def _cell_volt_bytes(*voltages_mv: int) -> bytes:
    out = b""
    for v in voltages_mv:
        out += v.to_bytes(2, "little")
    return out


def _power_current_bytes(*, current_a: float = 0.0, power_w: float = 0.0, cycles: int = 0) -> bytes:
    cur_raw = int(round(current_a / 0.001))
    if cur_raw < 0:
        cur_raw += 65536
    pwr_raw = int(round(power_w / 0.1))
    return bytes(
        [
            cur_raw & 0xFF, (cur_raw >> 8) & 0xFF,
            pwr_raw & 0xFF, (pwr_raw >> 8) & 0xFF,
            0, 0,
            cycles & 0xFF, (cycles >> 8) & 0xFF,
        ]
    )


# ---------- accumulator unit tests ----------


def test_accumulator_initial_state_insufficient() -> None:
    acc = CanFrameAccumulator()
    assert not acc.has_enough_for_snapshot()


def test_accumulator_main_status_alone_insufficient() -> None:
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    # We need at least one cell-voltage group too.
    assert not acc.has_enough_for_snapshot()


def test_accumulator_main_plus_cells_sufficient() -> None:
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(3300, 3300, 3300, 3300))
    assert acc.has_enough_for_snapshot()


def test_accumulator_to_live_data_uses_cell_voltages() -> None:
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes(53.0, 50, 0.0))
    # Cells 1..4 at 3.300, cells 5..16 zero (will be trimmed)
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(3300, 3300, 3300, 3300))
    live = acc.to_live_data()
    assert live.cell_voltages_v == pytest.approx((3.3, 3.3, 3.3, 3.3))
    assert live.total_voltage_v == pytest.approx(53.0)
    assert live.soc_percentage == 50


def test_accumulator_uses_cell_minmax_when_present() -> None:
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(3300, 3300, 3300, 3300))
    # max=3.65 at 1, min=3.20 at 5
    acc.apply(ID_CELL_MINMAX, bytes([0x42, 0x0E, 1, 0x80, 0x0C, 5, 0, 0]))
    live = acc.to_live_data()
    assert live.cell_voltage_max_v == pytest.approx(3.650)
    assert live.cell_voltage_min_v == pytest.approx(3.200)
    assert live.cell_voltage_delta_v == pytest.approx(0.450)
    assert live.cell_voltage_max_number == 1


def test_accumulator_power_current_overrides_main_status() -> None:
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(3300, 3300, 3300, 3300))
    acc.apply(ID_POWER_CURRENT, _power_current_bytes(current_a=10.0, power_w=530.0, cycles=42))
    live = acc.to_live_data()
    assert live.total_current_a == pytest.approx(10.0)
    assert live.total_power_w == pytest.approx(530.0)
    assert live.cycle_count == 42


def test_accumulator_individual_temps() -> None:
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(3300, 3300, 3300, 3300))
    acc.apply(ID_INDIVIDUAL_TEMPS, bytes([0, 70, 75, 0, 80, 0, 0, 0]))
    live = acc.to_live_data()
    assert live.mos_temp_c == pytest.approx(20.0)
    assert live.probe_1_temp_c == pytest.approx(25.0)
    # only 3 probes total → probe_2 should be 30.0, probe_3 stays default 0
    assert live.probe_2_temp_c == pytest.approx(30.0)


def test_accumulator_individual_temps_empty() -> None:
    """If individual_temps reports zero usable probes, the runner must not crash."""
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(3300, 3300, 3300, 3300))
    # All probe bytes zero → IndividualTemps.temperatures_c == ()
    acc.apply(ID_INDIVIDUAL_TEMPS, bytes(8))
    live = acc.to_live_data()
    # All probe fields stay at default 0.0
    assert live.mos_temp_c == 0.0
    assert live.probe_1_temp_c == 0.0
    assert live.probe_2_temp_c == 0.0
    assert live.probe_3_temp_c == 0.0
    assert live.probe_4_temp_c == 0.0


def test_accumulator_individual_temps_all_five() -> None:
    """With five probes the accumulator must populate all four probe_N fields."""
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(3300, 3300, 3300, 3300))
    # 5 probes after the leading 0 byte
    acc.apply(ID_INDIVIDUAL_TEMPS, bytes([0, 70, 75, 80, 65, 60, 0, 0]))
    live = acc.to_live_data()
    assert live.mos_temp_c == pytest.approx(20.0)
    assert live.probe_1_temp_c == pytest.approx(25.0)
    assert live.probe_2_temp_c == pytest.approx(30.0)
    assert live.probe_3_temp_c == pytest.approx(15.0)
    assert live.probe_4_temp_c == pytest.approx(10.0)


def test_accumulator_no_cells_returns_single_zero_cell() -> None:
    """Defensive: if cell voltages are all zero, we still produce a valid LiveData."""
    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(0, 0, 0, 0))
    live = acc.to_live_data()
    assert len(live.cell_voltages_v) == 1
    assert live.cell_voltages_v[0] == 0.0


def test_accumulator_temperatures_fallback() -> None:
    """If individual_temps not present, fall back to Temperatures min/max/avg."""
    from jkbms2mqtt.protocol.can_protocol import ID_TEMPERATURES

    acc = CanFrameAccumulator()
    acc.apply(ID_MAIN_STATUS, _main_status_bytes())
    acc.apply(ID_CELL_VOLT_BASE | (0 << 16), _cell_volt_bytes(3300, 3300, 3300, 3300))
    acc.apply(ID_TEMPERATURES, bytes([70, 1, 30, 4, 50, 0, 0, 0]))  # max=20, min=-20, avg=0
    live = acc.to_live_data()
    assert live.mos_temp_c == pytest.approx(0.0)
    assert live.probe_1_temp_c == pytest.approx(20.0)
    assert live.probe_2_temp_c == pytest.approx(-20.0)


def test_accumulator_ignores_unknown_ids() -> None:
    acc = CanFrameAccumulator()
    acc.apply(0xDEADBEEF, bytes(8))  # unknown id — must not raise
    assert not acc.has_enough_for_snapshot()


def test_accumulator_picks_up_alarm_and_other_frames() -> None:
    """Exercises the alarm-info branch."""
    from jkbms2mqtt.protocol.can_protocol import ID_ALARM_INFO

    acc = CanFrameAccumulator()
    acc.apply(ID_ALARM_INFO, bytes([0x01, 0, 0, 0, 0, 0, 0, 0]))
    assert acc.alarm_info is not None
    assert acc.alarm_info.alarms[0][1] == "Cell overvoltage"


def test_accumulator_monitoring_alias() -> None:
    """0x01F21400 frame populates the monitoring slot."""
    from jkbms2mqtt.protocol.can_protocol import ID_MONITORING

    acc = CanFrameAccumulator()
    acc.apply(ID_MONITORING, bytes([0xB4, 0x14, 0x64, 0x00, 0xFA, 0x00, 0x0C, 0x00]))
    assert acc.monitoring is not None
    assert acc.monitoring.total_voltage_v == pytest.approx(53.0)


# ---------- runner integration ----------


async def test_can_runner_publishes_discovery_and_live_snapshot() -> None:
    transport = FakeCanTransport(
        queue=deque(
            [
                CanMessage(ID_MAIN_STATUS, _main_status_bytes(53.0, 75, 5.0), True),
                CanMessage(
                    ID_CELL_VOLT_BASE | (0 << 16),
                    _cell_volt_bytes(3300, 3300, 3300, 3300),
                    True,
                ),
            ]
        )
    )
    mqtt = FakeMqttClient()
    runner = CanRunner(
        settings=_settings_can(),
        transport=transport,
        mqtt=mqtt,
        flush_interval_s=0.05,
    )
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        if any("JK_BMS_1/Total_Voltage_V" in t for t, *_ in mqtt.published):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    topics = [t for t, *_ in mqtt.published]
    # Discovery published
    assert any("homeassistant/sensor/jkbms_jk_bms_1/total_voltage/config" in t for t in topics)
    # Live data published
    assert any(t == "JK_BMS_1/Total_Voltage_V" for t in topics)
    assert any(t == "JK_BMS_1/SOC_percentage" for t in topics)


async def test_can_runner_skips_publish_when_incomplete() -> None:
    """If only MainStatus arrives (no cell voltages), no live publish should fire."""
    transport = FakeCanTransport(
        queue=deque(
            [CanMessage(ID_MAIN_STATUS, _main_status_bytes(), True)]
        )
    )
    mqtt = FakeMqttClient()
    runner = CanRunner(
        settings=_settings_can(),
        transport=transport,
        mqtt=mqtt,
        flush_interval_s=0.05,
    )
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    state_topics = [t for t, *_ in mqtt.published if "/config" not in t]
    assert not any(t == "JK_BMS_1/Total_Voltage_V" for t in state_topics)


async def test_can_runner_survives_decode_error(caplog) -> None:
    """A malformed CAN payload (wrong length) is logged but doesn't crash the loop."""
    import logging

    caplog.set_level(logging.DEBUG)
    bad = CanMessage(ID_MAIN_STATUS, b"\x00", True)  # wrong length
    good = CanMessage(ID_MAIN_STATUS, _main_status_bytes(), True)
    cells = CanMessage(ID_CELL_VOLT_BASE, _cell_volt_bytes(3300, 3300, 3300, 3300), True)
    transport = FakeCanTransport(queue=deque([bad, good, cells]))
    mqtt = FakeMqttClient()
    runner = CanRunner(
        settings=_settings_can(),
        transport=transport,
        mqtt=mqtt,
        flush_interval_s=0.05,
    )
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        if any("JK_BMS_1/Total_Voltage_V" in t for t, *_ in mqtt.published):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any("CAN frame decode error" in r.message for r in caplog.records)


async def test_can_runner_recovers_from_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class FlakyCanTransport:
        attempts: int = 0
        queue: deque[CanMessage] = field(default_factory=deque)

        @property
        def is_connected(self) -> bool:
            return True

        async def connect(self) -> None:  # pragma: no cover
            pass

        async def aclose(self) -> None:  # pragma: no cover
            pass

        async def recv_message(self, *, timeout_s: float) -> CanMessage | None:
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("simulated")
            if not self.queue:
                await asyncio.sleep(0)
                return None
            return self.queue.popleft()

    real_sleep = asyncio.sleep

    async def fast_sleep(d: float) -> None:
        if d >= 1:
            await real_sleep(0)
        else:
            await real_sleep(d)

    monkeypatch.setattr("jkbms2mqtt.can_runner.asyncio.sleep", fast_sleep)

    transport = FlakyCanTransport(
        queue=deque(
            [
                CanMessage(ID_MAIN_STATUS, _main_status_bytes(), True),
                CanMessage(ID_CELL_VOLT_BASE, _cell_volt_bytes(3300, 3300, 3300, 3300), True),
            ]
        )
    )
    mqtt = FakeMqttClient()
    runner = CanRunner(
        settings=_settings_can(),
        transport=transport,
        mqtt=mqtt,
        flush_interval_s=0.05,
    )
    task = asyncio.create_task(runner.run())
    for _ in range(200):
        if any("JK_BMS_1/Total_Voltage_V" in t for t, *_ in mqtt.published):
            break
        await real_sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert transport.attempts >= 2  # at least one error then recovery
