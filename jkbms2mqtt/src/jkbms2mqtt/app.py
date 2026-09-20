"""Orchestrator: ties pymodbus client, BmsRunners, MQTT, and WriteExecutor together.

For each configured slave_id we spawn one ``BmsRunner`` and one inbound MQTT
subscription. A single ``WriteExecutor`` task drains the shared write queue.
All of them share one ``pymodbus`` client.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from aiomqtt import Client as MqttClient
from aiomqtt import Will

from jkbms2mqtt import dashboard
from jkbms2mqtt.bms_runner import BmsRunner
from jkbms2mqtt.config import Settings, load_settings
from jkbms2mqtt.entities import writable_by_command_topic_suffix
from jkbms2mqtt.mqtt import (
    BRIDGE_AVAILABILITY_TOPIC,
    bridge_discovery_messages,
    orphan_removals,
    render,
    tier_state_messages,
)
from jkbms2mqtt.transport import ModbusClient, build_client, connect_with_backoff
from jkbms2mqtt.write_executor import WriteExecutor, WriteRequest
from jkbms2mqtt.write_ledger import WriteLedger

logger = logging.getLogger(__name__)

# Home Assistant's config dir, mounted into the add-on via the
# `homeassistant_config` map in config.yaml.
HA_CONFIG_DIR = Path("/homeassistant")


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


# How long to keep reading retained discovery configs. MQTT has no
# end-of-retained marker, so this is a settle window: collection stops once
# nothing new has arrived for this long.
DISCOVERY_SETTLE_S = 2.0


async def _clean_orphaned_discovery(  # pragma: no cover - MQTT glue
    mqtt: MqttClient, settings: Settings
) -> None:
    """Clear retained discovery configs of packs no longer in ``bms_ids``.

    Called before the command-topic subscriptions on purpose: ``mqtt.messages``
    is one shared queue, so draining it here while ``/set`` topics were already
    subscribed could swallow a user's write. Until we subscribe to them, no
    command can arrive.

    Which topics get cleared is decided by ``mqtt.orphan_removals`` — pure and
    unit-tested. This function only does the broker conversation.
    """
    wildcard = f"{settings.discovery_prefix}/+/+/config"
    await mqtt.subscribe(wildcard, qos=0)
    seen: list[str] = []
    messages = aiter(mqtt.messages)
    try:
        while True:
            message = await asyncio.wait_for(anext(messages), DISCOVERY_SETTLE_S)
            # An already-cleared config replays as an empty payload; skip it.
            if message.retain and message.payload:
                seen.append(str(message.topic))
    except TimeoutError:
        pass
    finally:
        await mqtt.unsubscribe(wildcard)

    removals = orphan_removals(seen, settings=settings)
    for removal in removals:
        topic, payload = render(removal)
        await mqtt.publish(topic, payload=payload, qos=1, retain=True)
        logger.info("cleared orphaned discovery config: %s", topic)
    logger.info(
        "orphaned-discovery cleanup: %d retained config(s) on the broker, %d cleared",
        len(seen),
        len(removals),
    )


# A dropped broker connection ends the session; aiomqtt does not reconnect on
# its own, so `run` rebuilds it with this backoff (#35).
MQTT_INITIAL_BACKOFF_S = 1.0
MQTT_MAX_BACKOFF_S = 30.0


async def _run_session(  # pragma: no cover - top-level glue
    settings: Settings,
    client: ModbusClient,
    ledger: WriteLedger,
    shutdown_event: asyncio.Event,
) -> None:
    """One MQTT connection's lifetime: publish, subscribe, poll until it ends.

    Returns normally only when shutdown was requested. Any other exit — the
    broker dropping, a poll task raising — propagates, so the caller can
    rebuild the session rather than limping on with dead tasks.

    The runners are constructed here on purpose: a reconnect therefore
    re-announces discovery and republishes every state topic, because a broker
    that restarted may have lost its retained set.
    """
    will = Will(topic=BRIDGE_AVAILABILITY_TOPIC, payload=b"offline", qos=1, retain=True)
    async with MqttClient(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_user or None,
        password=settings.mqtt_password or None,
        will=will,
    ) as mqtt:
        await mqtt.publish(BRIDGE_AVAILABILITY_TOPIC, b"online", qos=1, retain=True)

        # The write tiers, as retained state. They back the bridge's tier
        # sensors and gate the availability of every control, so publish them
        # before any discovery that references them.
        for topic, payload in tier_state_messages(settings):
            await mqtt.publish(topic, payload=payload, qos=1, retain=True)
        for message in bridge_discovery_messages(discovery_prefix=settings.discovery_prefix):
            config_topic, config_payload = render(message)
            await mqtt.publish(config_topic, payload=config_payload, qos=1, retain=True)

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
                ledger=ledger,
            )
            for sid in settings.bms_ids
        ]
        bms_by_name = {r.bms_name: r for r in runners}

        # Single write queue, single executor task
        write_queue: asyncio.Queue[WriteRequest] = asyncio.Queue()
        executor = WriteExecutor(
            client=client, settings=settings, publish=publish_write_output,
            ledger=ledger,
        )

        # Opt-in: clear retained discovery configs of packs that are no longer
        # configured. Must happen before the /set subscriptions below — see the
        # helper's docstring.
        if settings.clean_orphaned_discovery:
            await _clean_orphaned_discovery(mqtt, settings)

        # Always subscribe to every /set topic. The write executor enforces tier
        # gating and publishes a structured error to <bms>/error if a user posts
        # to a parameter whose tier is disabled by config — so the user gets
        # immediate, visible feedback instead of a silent drop.
        lookup = writable_by_command_topic_suffix()
        for r in runners:
            for suffix in lookup:
                await mqtt.subscribe(f"{r.bms_name}/{suffix}", qos=1)

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

        # Wait for whichever comes first: shutdown, or a task falling over.
        # Previously only `shutdown_event` was awaited, so a task that raised
        # died unnoticed — its exception sat unretrieved until the shutdown
        # gather swallowed it, and that pack silently stopped updating (#35).
        waiter = asyncio.create_task(shutdown_event.wait())
        try:
            done, _ = await asyncio.wait(
                [*tasks, waiter], return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (*tasks, waiter):
                task.cancel()
            await asyncio.gather(*tasks, waiter, return_exceptions=True)

        for task in done:
            if task is waiter or task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                raise exc
            raise RuntimeError("a bridge task exited unexpectedly")


async def run(settings: Settings) -> None:  # pragma: no cover - top-level glue
    """Run the bridge until SIGTERM / SIGINT, rebuilding the session as needed."""
    configure_logging(settings)

    client = build_client(settings)
    await connect_with_backoff(client)

    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)
    loop.add_signal_handler(signal.SIGINT, shutdown_event.set)

    # Outlives the MQTT session: a reconnect must not forget which parameters
    # were written, or the first poll afterwards could republish a stale one.
    ledger = WriteLedger()

    backoff = MQTT_INITIAL_BACKOFF_S
    try:
        while not shutdown_event.is_set():
            try:
                await _run_session(settings, client, ledger, shutdown_event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "bridge session ended (%s: %s) — restarting in %.1fs",
                    type(exc).__name__, exc, backoff,
                )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass  # backoff elapsed; go round again
                backoff = min(backoff * 2, MQTT_MAX_BACKOFF_S)
            else:
                backoff = MQTT_INITIAL_BACKOFF_S
    finally:
        client.close()


def _install_dashboard(  # pragma: no cover - add-on glue
    settings: Settings, config_dir: Path = HA_CONFIG_DIR
) -> None:
    """Write the auto-install dashboard + package into the HA config dir.

    Best-effort: a write failure (e.g. the homeassistant_config map is absent in
    a standalone container) is logged, never fatal. Uses one cell count for the
    whole bank. The dashboard is tier-agnostic: its rows switch between a
    setting's read-only twin and its control from the bridge's tier sensors,
    so a tier change needs no regeneration.
    """
    cells = {n: settings.dashboard_cells for n in settings.bms_ids}
    try:
        dash_path, pkg_path = dashboard.install(config_dir, settings.bms_ids, cells)
    except OSError as exc:
        logger.warning("install_dashboard: could not write dashboard files: %s", exc)
        return
    logger.info(
        "install_dashboard: wrote %s and %s — see DOCS.md for the one-time "
        "configuration.yaml block to show it in the sidebar",
        dash_path,
        pkg_path,
    )


def main() -> None:  # pragma: no cover - entrypoint
    settings = load_settings()
    configure_logging(settings)
    if settings.install_dashboard:
        _install_dashboard(settings)
    asyncio.run(run(settings))
