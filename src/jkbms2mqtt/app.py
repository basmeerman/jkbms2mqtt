"""Orchestrator: ties transport, codec, MQTT, and write executor together.

The runtime structure for a single BMS:

  ┌─────────────────────────────────────────┐
  │  per-BMS task                           │
  │    poll_loop()  ←─ asyncio.create_task  │
  │    write_executor.run(queue)            │
  │                                         │
  │  shared: transport, arbiter, MQTT client│
  └─────────────────────────────────────────┘

For multi-BMS configurations on the same bus (Modbus slave addresses 1..N),
all BMS share one transport, one arbiter, and one MQTT publisher; each BMS has
its own poll task and write queue. The arbiter ensures bus-level serialisation
across slaves.

This module is intentionally thin glue. Each subsystem is independently testable
and most of the bug-prone logic lives in `protocol/`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from aiomqtt import Client as MqttClient
from aiomqtt import Will

from jkbms2mqtt.bus_arbiter import BusArbiter
from jkbms2mqtt.config import Settings, load_settings
from jkbms2mqtt.entities import writable_by_command_topic_suffix
from jkbms2mqtt.mqtt import (
    build_discovery_messages,
    render,
    state_messages_from_fixed,
    state_messages_from_live,
    state_messages_from_setup,
)
from jkbms2mqtt.protocol.capabilities import Topology
from jkbms2mqtt.protocol.decoder import (
    LiveData,
    SetupData,
    decode_fixed,
    decode_live,
    decode_setup,
)
from jkbms2mqtt.protocol.jk_frame import FrameType, JkFrame, MalformedFrame, parse_jk_frame
from jkbms2mqtt.protocol.modbus import encode_poll_request
from jkbms2mqtt.protocol.registers import (
    POLL_TRIGGER_FIXED,
    POLL_TRIGGER_LIVE,
    POLL_TRIGGER_SETUP,
)
from jkbms2mqtt.recorder import RecordingTransport
from jkbms2mqtt.transport.base import Transport
from jkbms2mqtt.transport.can_bus import CanBusTransport
from jkbms2mqtt.transport.tcp_gateway import TcpGatewayTransport, connect_with_backoff
from jkbms2mqtt.write_executor import WriteExecutor, WriteRequest

logger = logging.getLogger(__name__)

JK_REPLY_LEN = 300


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","module":"%(name)s","msg":%(message)r}',
    )


def build_transport(settings: Settings) -> Transport | CanBusTransport:
    """Construct the configured transport.

    Supported transports:
    - `tcp_gateway` / `usb_serial` → `Transport`-shaped (byte-stream API).
    - `can_bus` → `CanBusTransport` (message-oriented; not a `Transport`).

    Either kind may be wrapped in `RecordingTransport` for replay logging.
    Only the byte-stream `Transport` kind is wrapped; `CanBusTransport` is
    handed directly to the CAN runner since it has its own message API.
    """
    from jkbms2mqtt.protocol.capabilities import Transport as TransportEnum
    from jkbms2mqtt.transport.usb_serial import UsbSerialTransport

    if settings.transport is TransportEnum.TCP_GATEWAY:
        assert settings.gateway_host and settings.gateway_port  # validator invariant
        byte_transport: Transport = TcpGatewayTransport(
            host=settings.gateway_host,
            port=settings.gateway_port,
        )
    elif settings.transport is TransportEnum.USB_SERIAL:
        assert settings.jkbms_path  # validator invariant
        byte_transport = UsbSerialTransport(device_path=settings.jkbms_path)
    elif settings.transport is TransportEnum.CAN_BUS:
        return CanBusTransport(channel=settings.jkbms_path or "can0")
    else:  # pragma: no cover - all enum members covered above
        raise NotImplementedError(
            f"transport={settings.transport.value} not yet implemented"
        )

    if settings.recording.enabled:
        path = Path(settings.recording.path) / "session.jsonl"
        return RecordingTransport(inner=byte_transport, path=path)
    return byte_transport


class BmsRunner:
    """One BMS's worth of state and tasks."""

    def __init__(
        self,
        *,
        settings: Settings,
        slave_addr: int,
        bms_name: str,
        transport: Transport,
        arbiter: BusArbiter,
        mqtt: MqttClient,
    ) -> None:
        self.settings = settings
        self.slave_addr = slave_addr
        self.bms_name = bms_name
        self.transport = transport
        self.arbiter = arbiter
        self.mqtt = mqtt
        self.write_queue: asyncio.Queue[WriteRequest] = asyncio.Queue()
        self.write_executor = WriteExecutor(
            transport=transport,
            arbiter=arbiter,
            settings=settings,
            publish=self._publish,
        )
        self._cell_count = 16  # updated from first setup frame
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def _publish(self, topic: str, payload: str) -> None:
        await self.mqtt.publish(topic, payload=payload, qos=1)

    async def announce_discovery(self) -> None:
        messages = build_discovery_messages(
            settings=self.settings,
            bms_name=self.bms_name,
            cell_count=self._cell_count,
        )
        for msg in messages:
            topic, payload = render(msg)
            await self.mqtt.publish(topic, payload=payload, qos=1, retain=True)

    async def poll_once(self, register: int) -> JkFrame | MalformedFrame:
        frame_req = encode_poll_request(self.slave_addr, register)
        async with self.arbiter.transaction():
            await self.transport.write(frame_req)
            raw = await self.transport.read_exactly(JK_REPLY_LEN, timeout_s=5.0)
        return parse_jk_frame(raw)

    async def poll_loop(self) -> None:
        """Round-robin between the three frame types.

        Live every `poll_interval_s`; setup and fixed every 10 polls.
        """
        cycle = 0
        while True:
            try:
                cycle += 1
                live_result = await self.poll_once(POLL_TRIGGER_LIVE)
                if isinstance(live_result, JkFrame) and live_result.frame_type is FrameType.LIVE:
                    self._publish_live(decode_live(live_result.raw, self._cell_count))

                if cycle % 10 == 1:
                    setup_result = await self.poll_once(POLL_TRIGGER_SETUP)
                    if isinstance(setup_result, JkFrame) and setup_result.frame_type is FrameType.SETUP:
                        setup = decode_setup(setup_result.raw)
                        self._cell_count = max(1, min(16, setup.cell_count))
                        self.write_executor.latest_setup = setup
                        await self._publish_setup(setup)
                if cycle == 1:
                    fixed_result = await self.poll_once(POLL_TRIGGER_FIXED)
                    if isinstance(fixed_result, JkFrame) and fixed_result.frame_type is FrameType.FIXED:
                        await self._publish_fixed(decode_fixed(fixed_result.raw))

            except (TimeoutError, ConnectionError) as exc:
                logger.warning("poll error on slave %d: %s — reconnecting", self.slave_addr, exc)
                await self._reconnect()
            await asyncio.sleep(self.settings.poll_interval_s)

    async def _reconnect(self) -> None:
        await self.transport.aclose()
        if isinstance(self.transport, RecordingTransport):
            inner = self.transport.inner
        else:
            inner = self.transport
        if isinstance(inner, TcpGatewayTransport):
            await connect_with_backoff(inner)

    def _publish_live(self, live: LiveData) -> None:
        # Schedule the publishes concurrently; keep a reference on the runner so
        # the task isn't GC'd mid-flight.
        task = asyncio.create_task(
            self._publish_many(state_messages_from_live(live, self.bms_name))
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _publish_setup(self, setup: SetupData) -> None:
        await self._publish_many(state_messages_from_setup(setup, self.bms_name))

    async def _publish_fixed(self, fixed: object) -> None:
        await self._publish_many(state_messages_from_fixed(fixed, self.bms_name))

    async def _publish_many(self, messages: list[tuple[str, str]]) -> None:
        for topic, payload in messages:
            await self.mqtt.publish(topic, payload=payload, qos=0)

    async def subscribe_writes(self) -> None:
        """Subscribe to every writable command topic for this BMS."""
        if not self.settings.writes_allowed_by_mode:
            return
        lookup = writable_by_command_topic_suffix()
        for suffix in lookup:
            topic = f"{self.bms_name}/{suffix}"
            await self.mqtt.subscribe(topic, qos=1)

    async def dispatch_message(self, topic: str, payload: bytes) -> None:
        """Route an inbound MQTT message to the write queue.

        Topics are of the form `<bms_name>/control/<param>/set`.
        """
        if not topic.startswith(f"{self.bms_name}/"):
            return
        suffix = topic[len(self.bms_name) + 1 :]
        lookup = writable_by_command_topic_suffix()
        entity = lookup.get(suffix)
        if entity is None:
            return
        await self.write_queue.put(
            WriteRequest(
                bms_name=self.bms_name,
                slave_addr=self.slave_addr,
                object_id=entity.object_id,
                raw_payload=payload.decode(errors="replace"),
            )
        )


async def run(settings: Settings) -> None:  # pragma: no cover - top-level glue
    """Run the bridge until SIGTERM.

    Dispatches to one of three runner topologies:
    - `master_poll` (TCP gateway or USB serial): per-BMS `BmsRunner` with poll_loop + write_executor.
    - `broadcast`: single `ListenRunner` demuxing by unit_no.
    - `can`: single `CanRunner` accumulating CAN frames into LiveData.
    """
    configure_logging(settings.log_level)

    transport = build_transport(settings)
    arbiter = BusArbiter(inter_frame_gap_ms=settings.inter_frame_gap_ms)

    await _connect_transport(transport)

    will = Will(topic="jkbms2mqtt/availability", payload=b"offline", qos=1, retain=True)
    async with MqttClient(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_user,
        password=settings.mqtt_password,
        will=will,
    ) as mqtt:
        await mqtt.publish("jkbms2mqtt/availability", b"online", qos=1, retain=True)

        loop = asyncio.get_event_loop()
        shutdown_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
        loop.add_signal_handler(signal.SIGINT, shutdown_event.set)

        tasks = await _spawn_runners(settings, transport, arbiter, mqtt)

        await shutdown_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    await transport.aclose()


async def _connect_transport(  # pragma: no cover - top-level glue
    transport: Transport | CanBusTransport,
) -> None:
    """Open the transport, applying transport-specific backoff."""
    from jkbms2mqtt.transport.can_bus import connect_with_backoff as can_connect
    from jkbms2mqtt.transport.usb_serial import UsbSerialTransport
    from jkbms2mqtt.transport.usb_serial import connect_with_backoff as serial_connect

    inner: Transport | CanBusTransport
    if isinstance(transport, RecordingTransport):
        inner = transport.inner
    else:
        inner = transport
    if isinstance(inner, TcpGatewayTransport):
        await connect_with_backoff(inner)
    elif isinstance(inner, UsbSerialTransport):
        await serial_connect(inner)
    elif isinstance(inner, CanBusTransport):
        await can_connect(inner)
    else:
        await transport.connect()


async def _spawn_runners(  # pragma: no cover - top-level glue
    settings: Settings,
    transport: Transport | CanBusTransport,
    arbiter: BusArbiter,
    mqtt: MqttClient,
) -> list[asyncio.Task[None]]:
    """Spawn the right set of background tasks for the current topology."""
    from jkbms2mqtt.can_runner import CanRunner
    from jkbms2mqtt.listen_runner import ListenRunner

    tasks: list[asyncio.Task[None]] = []

    if settings.topology is Topology.CAN:
        inner = transport.inner if isinstance(transport, RecordingTransport) else transport
        assert isinstance(inner, CanBusTransport)
        runner = CanRunner(settings=settings, transport=inner, mqtt=mqtt)
        tasks.append(asyncio.create_task(runner.run()))
        return tasks

    # Non-CAN topologies use the byte-stream Transport API.
    assert not isinstance(transport, CanBusTransport)

    if settings.topology is Topology.BROADCAST:
        listen = ListenRunner(settings=settings, transport=transport, mqtt=mqtt)
        tasks.append(asyncio.create_task(listen.run()))
        return tasks

    # master_poll: one BmsRunner per configured slave address.
    runners = [
        BmsRunner(
            settings=settings,
            slave_addr=i,
            bms_name=f"JK_BMS_{i}",
            transport=transport,
            arbiter=arbiter,
            mqtt=mqtt,
        )
        for i in range(1, settings.jkbms_count + 1)
    ]
    for r in runners:
        await r.announce_discovery()
        await r.subscribe_writes()
    for r in runners:
        tasks.append(asyncio.create_task(r.poll_loop()))
        tasks.append(asyncio.create_task(r.write_executor.run(r.write_queue)))

    async def dispatch() -> None:
        async for message in mqtt.messages:
            topic = str(message.topic)
            for r in runners:
                await r.dispatch_message(topic, bytes(message.payload))

    tasks.append(asyncio.create_task(dispatch()))
    return tasks


def main() -> None:  # pragma: no cover - entrypoint
    settings = load_settings()
    asyncio.run(run(settings))
