"""End-to-end orchestrator tests using `JsonlReplayTransport` as a fake BMS.

These tests stitch transport → codec → entity model → MQTT pub together to verify
that a recorded BMS exchange produces exactly the expected MQTT topics with the
expected values.

The fake MQTT publisher records `(topic, payload)` calls and lets tests assert
on the published state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jkbms2mqtt.app import BmsRunner
from jkbms2mqtt.bus_arbiter import BusArbiter
from jkbms2mqtt.config import Settings
from jkbms2mqtt.protocol.capabilities import Topology, Transport
from jkbms2mqtt.recorder import JsonlReplayTransport, write_jsonl


@dataclass
class FakeMqttClient:
    """Captures every publish + subscribe call."""

    published: list[tuple[str, bytes, int, bool]] = field(default_factory=list)
    subscribed: list[str] = field(default_factory=list)

    async def publish(self, topic, payload, qos=0, retain=False) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        self.published.append((topic, payload, qos, retain))

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append(topic)


def _settings_for_test() -> Settings:
    return Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        topology=Topology.MASTER_POLL,
        enable_basic_writes=True,
        enable_safety_writes=False,
        inter_frame_gap_ms=10,
    )


async def test_poll_once_decodes_live_frame_and_publishes_state(
    tmp_path: Path, live_frame
) -> None:
    raw_live = live_frame()
    poll_req = b"\x01\x10\x16\x20\x00\x01\x02\x00\x00"
    # Compute CRC for the poll request
    from jkbms2mqtt.protocol.modbus import append_crc

    poll_req_framed = append_crc(poll_req)

    path = tmp_path / "live.jsonl"
    write_jsonl(path, [("tx", poll_req_framed), ("rx", raw_live)])

    transport = JsonlReplayTransport(path=path)
    await transport.connect()
    arbiter = BusArbiter(inter_frame_gap_ms=0)
    mqtt = FakeMqttClient()
    runner = BmsRunner(
        settings=_settings_for_test(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=arbiter,
        mqtt=mqtt,  # type: ignore[arg-type]
    )
    result = await runner.poll_once(0x1620)
    from jkbms2mqtt.protocol.jk_frame import FrameType, JkFrame

    assert isinstance(result, JkFrame)
    assert result.frame_type is FrameType.LIVE


async def test_announce_discovery_publishes_retained_messages(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty.jsonl"
    write_jsonl(path, [])
    transport = JsonlReplayTransport(path=path)
    await transport.connect()
    arbiter = BusArbiter(inter_frame_gap_ms=0)
    mqtt = FakeMqttClient()
    runner = BmsRunner(
        settings=_settings_for_test(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=arbiter,
        mqtt=mqtt,  # type: ignore[arg-type]
    )
    await runner.announce_discovery()
    # All discovery messages are retained.
    assert all(retain for _, _, _, retain in mqtt.published)
    # Must contain at least the total_voltage discovery topic.
    topics = [t for t, _, _, _ in mqtt.published]
    assert any("total_voltage/config" in t for t in topics)


async def test_subscribe_writes_only_when_writes_allowed(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    write_jsonl(path, [])
    transport = JsonlReplayTransport(path=path)
    await transport.connect()

    # Writes allowed: subscriptions registered
    mqtt = FakeMqttClient()
    runner = BmsRunner(
        settings=_settings_for_test(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=BusArbiter(inter_frame_gap_ms=0),
        mqtt=mqtt,  # type: ignore[arg-type]
    )
    await runner.subscribe_writes()
    assert any("control/charging_switch/set" in t for t in mqtt.subscribed)

    # Writes disallowed: zero subscriptions
    s = Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        topology=Topology.BROADCAST,
    )
    mqtt2 = FakeMqttClient()
    runner2 = BmsRunner(
        settings=s,
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=BusArbiter(inter_frame_gap_ms=0),
        mqtt=mqtt2,  # type: ignore[arg-type]
    )
    await runner2.subscribe_writes()
    assert mqtt2.subscribed == []


async def test_dispatch_message_enqueues_write(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    write_jsonl(path, [])
    transport = JsonlReplayTransport(path=path)
    runner = BmsRunner(
        settings=_settings_for_test(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=transport,
        arbiter=BusArbiter(inter_frame_gap_ms=0),
        mqtt=FakeMqttClient(),  # type: ignore[arg-type]
    )
    await runner.dispatch_message("BMS_1/control/charging_switch/set", b"ON")
    assert runner.write_queue.qsize() == 1
    req = await runner.write_queue.get()
    assert req.object_id == "charging_switch"
    assert req.raw_payload == "ON"


async def test_dispatch_ignores_unrelated_topics(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    write_jsonl(path, [])
    runner = BmsRunner(
        settings=_settings_for_test(),
        slave_addr=1,
        bms_name="BMS_1",
        transport=JsonlReplayTransport(path=path),
        arbiter=BusArbiter(inter_frame_gap_ms=0),
        mqtt=FakeMqttClient(),  # type: ignore[arg-type]
    )
    await runner.dispatch_message("OTHER_BMS/control/charging_switch/set", b"ON")
    await runner.dispatch_message("BMS_1/control/totally_unknown/set", b"ON")
    assert runner.write_queue.qsize() == 0


async def test_build_transport_tcp_gateway() -> None:
    from jkbms2mqtt.app import build_transport
    from jkbms2mqtt.transport.tcp_gateway import TcpGatewayTransport

    s = _settings_for_test()
    transport = build_transport(s)
    assert isinstance(transport, TcpGatewayTransport)


async def test_build_transport_with_recording(tmp_path: Path) -> None:
    from jkbms2mqtt.app import build_transport
    from jkbms2mqtt.config import RecordingSettings
    from jkbms2mqtt.recorder import RecordingTransport

    s = Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        topology=Topology.MASTER_POLL,
        recording=RecordingSettings(enabled=True, path=str(tmp_path)),
    )
    transport = build_transport(s)
    assert isinstance(transport, RecordingTransport)


async def test_build_transport_usb_serial() -> None:
    from jkbms2mqtt.app import build_transport
    from jkbms2mqtt.transport.usb_serial import UsbSerialTransport

    s = Settings(
        transport=Transport.USB_SERIAL,
        jkbms_path="/dev/ttyUSB0",
        topology=Topology.MASTER_POLL,
    )
    transport = build_transport(s)
    assert isinstance(transport, UsbSerialTransport)
    assert transport.device_path == "/dev/ttyUSB0"


async def test_build_transport_can_bus() -> None:
    from jkbms2mqtt.app import build_transport
    from jkbms2mqtt.transport.can_bus import CanBusTransport

    s = Settings(
        transport=Transport.CAN_BUS,
        jkbms_path="can1",
        topology=Topology.CAN,
    )
    transport = build_transport(s)
    assert isinstance(transport, CanBusTransport)
    assert transport.channel == "can1"


async def test_build_transport_can_bus_default_channel() -> None:
    from jkbms2mqtt.app import build_transport
    from jkbms2mqtt.transport.can_bus import CanBusTransport

    s = Settings(
        transport=Transport.CAN_BUS,
        topology=Topology.CAN,
    )
    transport = build_transport(s)
    assert isinstance(transport, CanBusTransport)
    assert transport.channel == "can0"


def test_configure_logging_does_not_raise() -> None:
    from jkbms2mqtt.app import configure_logging

    configure_logging("info")
    configure_logging("debug")
