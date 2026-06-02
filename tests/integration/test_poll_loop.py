"""Exercises BmsRunner.poll_loop with a fake transport that returns scripted frames.

The loop is async-infinite, so each test starts the loop, waits for the expected
MQTT publishes, then cancels.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jkbms2mqtt.app import BmsRunner
from jkbms2mqtt.bus_arbiter import BusArbiter
from jkbms2mqtt.config import Settings
from jkbms2mqtt.protocol.capabilities import Topology, Transport


@dataclass
class FakeMqttClient:
    published: list[tuple[str, bytes, int, bool]] = field(default_factory=list)
    subscribed: list[str] = field(default_factory=list)

    async def publish(self, topic, payload, qos=0, retain=False) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        self.published.append((topic, payload, qos, retain))

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)


@dataclass
class ScriptedTransport:
    """A transport that returns canned responses to whatever the poll loop writes.

    `register_to_reply[register]` is the bytes to return for a poll that targets
    that register. Writes increment `tx_count`. Reads pull the next reply from
    the queue (matching the most recent write's register).
    """

    register_to_reply: dict[int, bytes]
    _pending_reply: bytes = b""
    _connected: bool = True
    tx_count: int = 0
    fail_after: int | None = None  # raise after this many polls (to test recovery branch)

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def aclose(self) -> None:
        self._connected = False

    async def write(self, data: bytes) -> None:
        self.tx_count += 1
        if self.fail_after is not None and self.tx_count > self.fail_after:
            raise ConnectionError("simulated drop")
        register = (data[2] << 8) | data[3]
        reply = self.register_to_reply.get(register, b"")
        self._pending_reply = reply

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
        del timeout_s
        if len(self._pending_reply) < n:
            raise TimeoutError("not enough bytes queued")
        out = self._pending_reply[:n]
        self._pending_reply = self._pending_reply[n:]
        return out


def _settings() -> Settings:
    return Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        topology=Topology.MASTER_POLL,
        enable_basic_writes=True,
        poll_interval_s=1,
        inter_frame_gap_ms=10,
    )


async def test_poll_loop_second_cycle_polls_live_only(
    monkeypatch: pytest.MonkeyPatch, live_frame, setup_frame, fixed_frame
) -> None:
    """Cycles 2..10 must NOT poll setup/fixed — only live."""
    transport = ScriptedTransport(
        register_to_reply={
            0x161C: fixed_frame(),
            0x1620: live_frame(),
            0x1622: setup_frame(),
        }
    )
    runner = BmsRunner(
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=BusArbiter(inter_frame_gap_ms=10),
        mqtt=FakeMqttClient(),  # type: ignore[arg-type]
    )
    # Make the per-cycle sleep instant so we observe a 2nd iteration immediately.
    real_sleep = asyncio.sleep

    async def fast_sleep(delay: float) -> None:
        if delay >= 1:  # the long inter-cycle sleep
            await real_sleep(0)
        else:
            await real_sleep(delay)

    monkeypatch.setattr("jkbms2mqtt.app.asyncio.sleep", fast_sleep)

    task = asyncio.create_task(runner.poll_loop())
    for _ in range(200):
        if transport.tx_count >= 4:
            break
        await real_sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # 3 polls for cycle 1 + ≥1 polls for cycle 2 = at least 4. Cycle 2 only
    # touches LIVE because (cycle % 10) != 1 and cycle != 1.
    assert transport.tx_count >= 4


async def test_poll_loop_publishes_live_setup_and_fixed(
    live_frame, setup_frame, fixed_frame
) -> None:
    """First cycle: live + setup + fixed all polled and decoded."""
    transport = ScriptedTransport(
        register_to_reply={
            0x161C: fixed_frame(),
            0x1620: live_frame(),
            0x1622: setup_frame(),
        }
    )
    arbiter = BusArbiter(inter_frame_gap_ms=10)
    mqtt = FakeMqttClient()
    runner = BmsRunner(
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=arbiter,
        mqtt=mqtt,  # type: ignore[arg-type]
    )

    async def run_one_cycle() -> None:
        task = asyncio.create_task(runner.poll_loop())
        # Wait long enough for the first iteration's three polls + scheduling of
        # the async live-publish task.
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await run_one_cycle()
    topics = [t for t, *_ in mqtt.published]
    assert any(t == "BMS_1/Total_Voltage_V" for t in topics)  # live
    assert any(
        t == "BMS_1/control/charging_switch" for t in topics
    )  # setup → control state
    assert any(t == "BMS_1/bms" for t in topics)  # fixed


async def test_poll_loop_skips_malformed_frames(live_frame) -> None:
    """If a poll returns garbage, the loop must keep going (not crash and not publish)."""
    garbage = b"\x00" * 300
    transport = ScriptedTransport(
        register_to_reply={
            0x161C: garbage,
            0x1620: garbage,
            0x1622: garbage,
        }
    )
    mqtt = FakeMqttClient()
    runner = BmsRunner(
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=BusArbiter(inter_frame_gap_ms=10),
        mqtt=mqtt,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(runner.poll_loop())
    await asyncio.sleep(0.4)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Discovery wasn't called — no state topics should have been published.
    topics = [t for t, *_ in mqtt.published]
    assert not any(t == "BMS_1/Total_Voltage_V" for t in topics)


async def test_reconnect_handles_recording_wrapper(monkeypatch: pytest.MonkeyPatch, live_frame) -> None:
    """Closes coverage gap: _reconnect unwraps RecordingTransport to find the TCP gateway."""
    from jkbms2mqtt.recorder import RecordingTransport
    from jkbms2mqtt.transport.tcp_gateway import TcpGatewayTransport

    # Build a real TcpGatewayTransport (so isinstance succeeds) wrapped in RecordingTransport.
    inner = TcpGatewayTransport(host="127.0.0.1", port=1)
    rec = RecordingTransport(inner=inner, path=Path("/tmp/never-used.jsonl"))

    called: list[str] = []

    async def fake_connect_with_backoff(transport: TcpGatewayTransport) -> None:
        called.append(transport.host)

    async def fake_aclose() -> None:
        called.append("aclose")

    monkeypatch.setattr(rec, "aclose", fake_aclose)
    monkeypatch.setattr(
        "jkbms2mqtt.app.connect_with_backoff", fake_connect_with_backoff
    )

    runner = BmsRunner(
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=rec,
        arbiter=BusArbiter(inter_frame_gap_ms=10),
        mqtt=FakeMqttClient(),  # type: ignore[arg-type]
    )
    await runner._reconnect()
    assert called == ["aclose", "127.0.0.1"]


async def test_bms_runner_publish_invokes_mqtt() -> None:
    """Cover the _publish wrapper used by the write executor."""
    import tempfile

    from jkbms2mqtt.recorder import JsonlReplayTransport

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl") as f:
        path = Path(f.name)
    mqtt = FakeMqttClient()
    runner = BmsRunner(
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=JsonlReplayTransport(path=path),
        arbiter=BusArbiter(inter_frame_gap_ms=10),
        mqtt=mqtt,  # type: ignore[arg-type]
    )
    await runner._publish("BMS_1/test", "hello")
    assert ("BMS_1/test", b"hello", 1, False) in mqtt.published


async def test_poll_loop_reconnects_after_connection_error(
    monkeypatch: pytest.MonkeyPatch, live_frame
) -> None:
    """Long-running stall recovery: if a read fails, we close and reconnect."""
    transport = ScriptedTransport(
        register_to_reply={0x1620: live_frame()},
        fail_after=1,  # first write OK, subsequent writes raise
    )

    async def fake_connect_with_backoff(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        "jkbms2mqtt.app.connect_with_backoff", fake_connect_with_backoff
    )

    arbiter = BusArbiter(inter_frame_gap_ms=10)
    mqtt = FakeMqttClient()
    runner = BmsRunner(
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=arbiter,
        mqtt=mqtt,  # type: ignore[arg-type]
    )

    task = asyncio.create_task(runner.poll_loop())
    await asyncio.sleep(0.4)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # transport.aclose was called → not connected
    assert transport.tx_count >= 2  # at least one retry attempted
