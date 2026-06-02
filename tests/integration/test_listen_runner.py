"""End-to-end tests for the broadcast/listen runner.

A fake transport surfaces a scripted byte stream as if multiple BMSes were
broadcasting. We assert that:
- Frames split correctly across read chunks.
- Each unique `unit_no` triggers HA Discovery exactly once.
- Per-frame state goes to the right `<JK_BMS_N>` topic.
- Multi-BMS broadcast: three units on one bus → three HA devices, three
  distinct state-topic streams.
"""

from __future__ import annotations

import asyncio
import struct
from collections import deque
from dataclasses import dataclass, field

import pytest

from jkbms2mqtt.config import Settings
from jkbms2mqtt.listen_runner import ListenRunner
from jkbms2mqtt.protocol.capabilities import Topology, Transport
from jkbms2mqtt.protocol.jk_frame import MAGIC, FrameType, compute_checksum


@dataclass
class FakeMqttClient:
    published: list[tuple[str, bytes, int, bool]] = field(default_factory=list)

    async def publish(self, topic, payload, qos=0, retain=False) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        self.published.append((topic, payload, qos, retain))


@dataclass
class FakeStreamTransport:
    """Surfaces a list of chunks as if read from an RS485 line."""

    chunks: deque[bytes] = field(default_factory=deque)
    _connected: bool = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def aclose(self) -> None:
        self._connected = False

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
        del timeout_s
        # Yield to the loop so cancellation can fire even on a tight read sequence.
        await asyncio.sleep(0)
        out = b""
        while len(out) < n and self.chunks:
            out += self.chunks.popleft()
        if not out:
            # Simulate quiet bus — nothing to read this period.
            raise TimeoutError("no data")
        # If we read more than requested, push the remainder back.
        if len(out) > n:
            self.chunks.appendleft(out[n:])
            out = out[:n]
        return out

    async def write(self, data: bytes) -> None:  # pragma: no cover - listen-only
        pass


def _make_frame(frame_type: FrameType, *, unit_no: int) -> bytes:
    """Build a minimal-but-valid 300-byte JK frame for the given unit_no."""
    buf = bytearray(300)
    buf[0:4] = MAGIC
    buf[4] = int(frame_type)
    # Put a basic Trame 3 layout if this is LIVE, else just zeros.
    if frame_type is FrameType.LIVE:
        # 16 cell voltages at 3.3 V
        for i in range(16):
            struct.pack_into("<H", buf, 6 + 2 * i, 3300)
        struct.pack_into("<H", buf, 234, 5300)  # total voltage 53.00 V
        struct.pack_into("<i", buf, 158, 0)  # total current 0
        struct.pack_into("<I", buf, 154, 0)  # power
        buf[173] = 50  # SOC 50%
        buf[190] = 100  # SOH
    elif frame_type is FrameType.SETUP:
        struct.pack_into("<i", buf, 114, 16)  # cell_count
    # unit_no at offset (300-5)
    struct.pack_into("<I", buf, 295, unit_no)
    buf[-1] = compute_checksum(bytes(buf[:-1]))
    return bytes(buf)


def _settings_broadcast() -> Settings:
    return Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        topology=Topology.BROADCAST,
        jkbms_count=3,
    )


async def _run_for(runner: ListenRunner, *, until: callable, max_iter: int = 200) -> None:
    task = asyncio.create_task(runner.run())
    for _ in range(max_iter):
        if until():
            break
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_single_unit_publishes_discovery_and_state() -> None:
    frame = _make_frame(FrameType.LIVE, unit_no=1)
    transport = FakeStreamTransport(chunks=deque([frame]))
    mqtt = FakeMqttClient()
    runner = ListenRunner(settings=_settings_broadcast(), transport=transport, mqtt=mqtt)
    await _run_for(runner, until=lambda: any("JK_BMS_1/Total_Voltage_V" in t for t, *_ in mqtt.published))
    topics = [t for t, *_ in mqtt.published]
    # Discovery for unit 1 went out
    assert any("homeassistant/sensor/jkbms_jk_bms_1" in t for t in topics)
    # Live state too
    assert any(t == "JK_BMS_1/Total_Voltage_V" for t in topics)


async def test_multi_unit_broadcast_each_gets_distinct_discovery() -> None:
    """Three BMSes broadcasting on one bus → three discovery streams."""
    frames = b"".join(
        _make_frame(FrameType.LIVE, unit_no=u) for u in (1, 2, 3)
    )
    transport = FakeStreamTransport(chunks=deque([frames]))
    mqtt = FakeMqttClient()
    runner = ListenRunner(settings=_settings_broadcast(), transport=transport, mqtt=mqtt)
    await _run_for(
        runner,
        until=lambda: all(
            any(f"JK_BMS_{u}/Total_Voltage_V" in t for t, *_ in mqtt.published)
            for u in (1, 2, 3)
        ),
        max_iter=500,
    )
    topics = [t for t, *_ in mqtt.published]
    # Three distinct devices in discovery
    assert any("jkbms_jk_bms_1/total_voltage/config" in t for t in topics)
    assert any("jkbms_jk_bms_2/total_voltage/config" in t for t in topics)
    assert any("jkbms_jk_bms_3/total_voltage/config" in t for t in topics)


async def test_repeated_frames_for_same_unit_do_not_redo_discovery() -> None:
    """Subsequent frames for an already-known unit must not re-announce HA Discovery."""
    frames = _make_frame(FrameType.LIVE, unit_no=1) + _make_frame(FrameType.LIVE, unit_no=1)
    transport = FakeStreamTransport(chunks=deque([frames]))
    mqtt = FakeMqttClient()
    runner = ListenRunner(settings=_settings_broadcast(), transport=transport, mqtt=mqtt)
    await _run_for(
        runner,
        until=lambda: sum(1 for t, *_ in mqtt.published if t == "JK_BMS_1/Total_Voltage_V") >= 2,
        max_iter=500,
    )
    # Total_Voltage published twice
    assert sum(1 for t, *_ in mqtt.published if t == "JK_BMS_1/Total_Voltage_V") == 2
    # But discovery (sensor/config) only once per topic
    discovery_topics = [t for t, *_ in mqtt.published if t.endswith("/config")]
    assert len(discovery_topics) == len(set(discovery_topics))


async def test_setup_frame_updates_cell_count() -> None:
    setup = _make_frame(FrameType.SETUP, unit_no=1)
    transport = FakeStreamTransport(chunks=deque([setup]))
    mqtt = FakeMqttClient()
    runner = ListenRunner(settings=_settings_broadcast(), transport=transport, mqtt=mqtt)
    await _run_for(runner, until=lambda: bool(mqtt.published), max_iter=200)
    # Setup frame produces control-state publishes
    assert any("JK_BMS_1/control/charging_switch" in t for t, *_ in mqtt.published)


async def test_fixed_frame_publishes_static_info() -> None:
    fixed = _make_frame(FrameType.FIXED, unit_no=1)
    transport = FakeStreamTransport(chunks=deque([fixed]))
    mqtt = FakeMqttClient()
    runner = ListenRunner(settings=_settings_broadcast(), transport=transport, mqtt=mqtt)
    await _run_for(runner, until=lambda: any(t.endswith("/bms") for t, *_ in mqtt.published))
    assert any(t == "JK_BMS_1/bms" for t, *_ in mqtt.published)


async def test_malformed_frame_is_logged_and_skipped(caplog) -> None:
    """A corrupted-magic frame must not crash the loop."""
    import logging

    caplog.set_level(logging.DEBUG)
    # Mangle the type byte to an unknown value
    raw = bytearray(_make_frame(FrameType.LIVE, unit_no=1))
    raw[4] = 0xFF
    raw[-1] = compute_checksum(bytes(raw[:-1]))
    # Follow with a valid frame to ensure the loop survives.
    transport = FakeStreamTransport(chunks=deque([bytes(raw) + _make_frame(FrameType.LIVE, unit_no=2)]))
    mqtt = FakeMqttClient()
    runner = ListenRunner(settings=_settings_broadcast(), transport=transport, mqtt=mqtt)
    await _run_for(
        runner,
        until=lambda: any("JK_BMS_2/Total_Voltage_V" in t for t, *_ in mqtt.published),
    )
    assert any("malformed frame" in r.message for r in caplog.records)


async def test_transport_timeout_keeps_loop_alive() -> None:
    transport = FakeStreamTransport(chunks=deque())  # always raises TimeoutError
    # Queue one frame after a few timeouts.
    mqtt = FakeMqttClient()
    runner = ListenRunner(settings=_settings_broadcast(), transport=transport, mqtt=mqtt)
    task = asyncio.create_task(runner.run())
    # Let the loop spin and accumulate timeouts.
    await asyncio.sleep(0.05)
    transport.chunks.append(_make_frame(FrameType.LIVE, unit_no=1))
    for _ in range(200):
        if any("JK_BMS_1/Total_Voltage_V" in t for t, *_ in mqtt.published):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any(t == "JK_BMS_1/Total_Voltage_V" for t, *_ in mqtt.published)


async def test_transport_connection_error_keeps_loop_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    @dataclass
    class FlakyTransport:
        attempts: int = 0
        next_chunk: bytes = b""

        @property
        def is_connected(self) -> bool:
            return True

        async def connect(self) -> None:  # pragma: no cover
            pass

        async def aclose(self) -> None:  # pragma: no cover
            pass

        async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("simulated drop")
            if not self.next_chunk:
                raise TimeoutError("idle")
            chunk, self.next_chunk = self.next_chunk, b""
            return chunk

        async def write(self, data: bytes) -> None:  # pragma: no cover
            pass

    # Make the 1s recovery sleep effectively instant.
    real_sleep = asyncio.sleep

    async def fast_sleep(d: float) -> None:
        if d >= 1:
            await real_sleep(0)
        else:
            await real_sleep(d)

    monkeypatch.setattr("jkbms2mqtt.listen_runner.asyncio.sleep", fast_sleep)
    transport = FlakyTransport(next_chunk=_make_frame(FrameType.LIVE, unit_no=1))
    mqtt = FakeMqttClient()
    runner = ListenRunner(settings=_settings_broadcast(), transport=transport, mqtt=mqtt)
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
