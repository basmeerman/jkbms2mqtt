"""Broadcast / listen runner.

In broadcast topology one of the JK-BMSes is the bus master; the rest reply
when polled. We just listen to the bus, frame-split the stream, and demultiplex
by the BMS-reported `unit_no` field embedded in each reply frame.

This runner lazily creates one `BmsRunner`-style state per `unit_no` it sees,
so multi-BMS broadcast setups work without any per-BMS configuration. The
orchestrator hands over the MQTT client + Settings; per-BMS HA Discovery is
published the first time a new unit_no appears.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jkbms2mqtt.mqtt import (
    build_discovery_messages,
    render,
    state_messages_from_fixed,
    state_messages_from_live,
    state_messages_from_setup,
)
from jkbms2mqtt.protocol.decoder import decode_fixed, decode_live, decode_setup
from jkbms2mqtt.protocol.jk_frame import FrameType, JkFrame, MalformedFrame, parse_jk_frame
from jkbms2mqtt.protocol.scanner import FrameScanner

if TYPE_CHECKING:  # pragma: no cover
    from jkbms2mqtt.config import Settings
    from jkbms2mqtt.transport.base import Transport

logger = logging.getLogger(__name__)

DEFAULT_READ_CHUNK = 64  # small enough to keep latency low on quiet buses
READ_TIMEOUT_S = 2.0


@dataclass
class _UnitState:
    """Per-discovered-BMS state held by the listen runner."""

    unit_no: int
    bms_name: str
    cell_count: int = 16
    discovered: bool = False


@dataclass
class ListenRunner:
    """One listen loop per bus.

    Frames are demultiplexed by `unit_no`; HA discovery is published the first
    time a given unit_no is seen, and per-frame state is published on the
    standard topic suffixes.
    """

    settings: Settings
    transport: Transport
    mqtt: object  # any aiomqtt-shaped client
    bms_name_prefix: str = "JK_BMS"

    _scanner: FrameScanner = field(default_factory=FrameScanner, init=False)
    _units: dict[int, _UnitState] = field(default_factory=dict, init=False)
    _announce_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)

    async def run(self) -> None:
        """Read bytes forever, dispatch frames as they complete.

        We read in smallish chunks (`DEFAULT_READ_CHUNK`) so that even a quiet
        bus doesn't make us block for a full frame's worth of bytes. The 50 ms
        sleep on TimeoutError keeps idle CPU near zero and gives the asyncio
        loop a chance to process cancellation.
        """
        while True:
            try:
                chunk = await self.transport.read_exactly(
                    DEFAULT_READ_CHUNK, timeout_s=READ_TIMEOUT_S
                )
            except TimeoutError:
                # No data — keep listening. Yield so other tasks (and cancel) progress.
                await asyncio.sleep(0.05)
                continue
            except ConnectionError as exc:
                logger.warning("listen-loop transport error: %s", exc)
                await asyncio.sleep(1.0)
                continue

            for raw_frame in self._scanner.feed(chunk):
                await self._dispatch(raw_frame)

    async def _dispatch(self, raw: bytes) -> None:
        result = parse_jk_frame(raw)
        if isinstance(result, MalformedFrame):
            logger.debug("malformed frame from bus: %s", result.reason)
            return
        await self._handle_frame(result)

    async def _handle_frame(self, frame: JkFrame) -> None:
        unit = self._unit_for(frame.unit_no)
        if frame.frame_type is FrameType.LIVE:
            live = decode_live(frame.raw, unit.cell_count)
            await self._publish_many(state_messages_from_live(live, unit.bms_name))
        elif frame.frame_type is FrameType.SETUP:
            setup = decode_setup(frame.raw)
            unit.cell_count = max(1, min(16, setup.cell_count))
            await self._publish_many(state_messages_from_setup(setup, unit.bms_name))
        else:
            # frame.frame_type is FrameType.FIXED — parse_jk_frame guarantees one of three.
            fixed = decode_fixed(frame.raw)
            await self._publish_many(state_messages_from_fixed(fixed, unit.bms_name))

    def _unit_for(self, unit_no: int) -> _UnitState:
        state = self._units.get(unit_no)
        if state is None:
            state = _UnitState(
                unit_no=unit_no,
                bms_name=f"{self.bms_name_prefix}_{unit_no}",
            )
            self._units[unit_no] = state
        if not state.discovered:
            # Mark synchronously so a burst of frames for the same unit only
            # schedules one announce task. Hold the task reference so it isn't GC'd.
            state.discovered = True
            task = asyncio.create_task(self._announce(state))
            self._announce_tasks.add(task)
            task.add_done_callback(self._announce_tasks.discard)
        return state

    async def _announce(self, state: _UnitState) -> None:
        messages = build_discovery_messages(
            settings=self.settings,
            bms_name=state.bms_name,
            cell_count=state.cell_count,
        )
        for msg in messages:
            topic, payload = render(msg)
            await self.mqtt.publish(topic, payload=payload, qos=1, retain=True)  # type: ignore[attr-defined]

    async def _publish_many(self, messages: list[tuple[str, str]]) -> None:
        for topic, payload in messages:
            await self.mqtt.publish(topic, payload=payload, qos=0)  # type: ignore[attr-defined]
