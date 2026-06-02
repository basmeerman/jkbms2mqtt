"""Orchestrator: ties pymodbus client, BmsRunners, MQTT, and WriteExecutor together.

For each configured slave_id we spawn one ``BmsRunner`` and one inbound MQTT
subscription. A single ``WriteExecutor`` task drains the shared write queue.
All of them share one ``pymodbus`` client.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiomqtt import Client as MqttClient
from aiomqtt import Will

from jkbms2mqtt.bms_runner import BmsRunner
from jkbms2mqtt.config import Settings, load_settings
from jkbms2mqtt.entities import writable_by_command_topic_suffix
from jkbms2mqtt.transport import build_client, connect_with_backoff
from jkbms2mqtt.write_executor import WriteExecutor, WriteRequest

logger = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    """Set up root logging.

    ``force=True`` is essential — it overrides any handler that ``aiomqtt`` or
    ``pymodbus`` may have attached before we got here. Without it the
    ``basicConfig`` call is a no-op once those libraries have touched the root
    logger, and DEBUG output disappears even when the user asks for it.
    """
    logging.basicConfig(
        level=getattr(logging, settings.log_level.value.upper()),
        format=(
            '{"ts":"%(asctime)s","level":"%(levelname)s",'
            '"module":"%(name)s","msg":%(message)r}'
        ),
        force=True,
    )
    if settings.recording_enabled:
        # Route pymodbus' transaction-level hex dumps to our log pipeline.
        logging.getLogger("pymodbus").setLevel(logging.DEBUG)


async def run(settings: Settings) -> None:  # pragma: no cover - top-level glue
    """Run the bridge until SIGTERM / SIGINT."""
    configure_logging(settings)

    client = build_client(settings)
    await connect_with_backoff(client)

    will = Will(topic="jkbms2mqtt/availability", payload=b"offline", qos=1, retain=True)
    async with MqttClient(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_user or None,
        password=settings.mqtt_password or None,
        will=will,
    ) as mqtt:
        await mqtt.publish("jkbms2mqtt/availability", b"online", qos=1, retain=True)

        async def publish(topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
            await mqtt.publish(topic, payload=payload, qos=qos, retain=retain)

        async def publish_write_output(topic: str, payload: str) -> None:
            await publish(topic, payload, qos=1, retain=False)

        # Per-BMS runners
        runners = [
            BmsRunner(
                client=client,
                settings=settings,
                slave_addr=sid,
                bms_name=f"{settings.bms_name_prefix}_{sid}",
                publish=publish,
            )
            for sid in settings.bms_ids
        ]
        bms_by_name = {r.bms_name: r for r in runners}

        # Single write queue, single executor task
        write_queue: asyncio.Queue[WriteRequest] = asyncio.Queue()
        executor = WriteExecutor(
            client=client, settings=settings, publish=publish_write_output
        )

        # Subscriptions
        if settings.enable_basic_writes or settings.enable_safety_writes:
            lookup = writable_by_command_topic_suffix()
            for r in runners:
                for suffix in lookup:
                    await mqtt.subscribe(f"{r.bms_name}/{suffix}", qos=1)

        # Signal handling
        loop = asyncio.get_event_loop()
        shutdown_event = asyncio.Event()
        loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
        loop.add_signal_handler(signal.SIGINT, shutdown_event.set)

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(r.poll_loop()) for r in runners
        ]
        tasks.append(asyncio.create_task(executor.run(write_queue)))

        async def dispatch() -> None:
            lookup = writable_by_command_topic_suffix()
            async for message in mqtt.messages:
                topic = str(message.topic)
                bms_name, _, suffix = topic.partition("/")
                runner = bms_by_name.get(bms_name)
                if runner is None:
                    continue
                entity = lookup.get(suffix)
                if entity is None:
                    continue
                await write_queue.put(
                    WriteRequest(
                        bms_name=bms_name,
                        slave_addr=runner.slave_addr,
                        object_id=entity.object_id,
                        raw_payload=bytes(message.payload).decode(errors="replace"),
                    )
                )

        tasks.append(asyncio.create_task(dispatch()))

        await shutdown_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    client.close()


def main() -> None:  # pragma: no cover - entrypoint
    settings = load_settings()
    asyncio.run(run(settings))
