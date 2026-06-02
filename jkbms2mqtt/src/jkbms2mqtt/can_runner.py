"""CAN bus runner: receives JK CAN frames, accumulates a LiveData snapshot, publishes.

The JK CAN protocol spreads pack telemetry across ~10 different CAN IDs (one
for total voltage / SoC / current, another for cell min/max, four for cell
voltages 1..16, etc.). We accumulate one frame of each type and emit a
combined `LiveData` snapshot to MQTT every `flush_interval_s`.

Writes are not supported (capability matrix already prevents them). HA
Discovery for writable entities is skipped — only sensors / binary sensors
appear in HA when `topology=can`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jkbms2mqtt.mqtt import build_discovery_messages, render, state_messages_from_live
from jkbms2mqtt.protocol.can_protocol import (
    ID_ALARM_INFO,
    ID_CELL_MINMAX,
    ID_INDIVIDUAL_TEMPS,
    ID_MAIN_STATUS,
    ID_MONITORING,
    ID_POWER_CURRENT,
    ID_TEMPERATURES,
    AlarmInfo,
    CellMinMax,
    CellVoltages,
    IndividualTemps,
    MainStatus,
    Monitoring,
    PowerCurrent,
    Temperatures,
    decode_alarm_info,
    decode_cell_minmax,
    decode_cell_voltages,
    decode_individual_temps,
    decode_main_status,
    decode_monitoring,
    decode_power_current,
    decode_temperatures,
    is_cell_voltage_id,
)
from jkbms2mqtt.protocol.decoder import LiveData

if TYPE_CHECKING:  # pragma: no cover
    from jkbms2mqtt.config import Settings
    from jkbms2mqtt.transport.can_bus import CanBusTransport

logger = logging.getLogger(__name__)

RECV_TIMEOUT_S = 1.0
DEFAULT_FLUSH_INTERVAL_S = 5.0


@dataclass
class CanFrameAccumulator:
    """Cumulative state across one cycle of incoming CAN frames.

    `try_snapshot()` returns a `LiveData` if enough fields have been collected
    to be useful, else None. We treat the main status (0x2F4) as the trigger
    field — once we have it, plus any cell voltages, we can publish.
    """

    main_status: MainStatus | None = None
    cell_minmax: CellMinMax | None = None
    temperatures: Temperatures | None = None
    power_current: PowerCurrent | None = None
    monitoring: Monitoring | None = None
    individual_temps: IndividualTemps | None = None
    alarm_info: AlarmInfo | None = None
    cell_voltages_by_group: dict[int, CellVoltages] = field(default_factory=dict)

    def apply(self, can_id: int, data: bytes) -> None:
        """Decode and store the right fragment based on `can_id`."""
        if can_id == ID_MAIN_STATUS:
            self.main_status = decode_main_status(data)
        elif can_id == ID_CELL_MINMAX:
            self.cell_minmax = decode_cell_minmax(data)
        elif can_id == ID_TEMPERATURES:
            self.temperatures = decode_temperatures(data)
        elif can_id == ID_POWER_CURRENT:
            self.power_current = decode_power_current(data)
        elif can_id == ID_MONITORING:
            self.monitoring = decode_monitoring(data)
        elif can_id == ID_INDIVIDUAL_TEMPS:
            self.individual_temps = decode_individual_temps(data)
        elif can_id == ID_ALARM_INFO:
            self.alarm_info = decode_alarm_info(data)
        elif is_cell_voltage_id(can_id):
            cv = decode_cell_voltages(can_id, data)
            self.cell_voltages_by_group[cv.group_index] = cv
        # Unknown IDs (0x18F528F4, 0x1806E5F4, 0x18F428F4) are read but not yet mapped to
        # any entity; ignoring them is safe.

    def has_enough_for_snapshot(self) -> bool:
        return self.main_status is not None and bool(self.cell_voltages_by_group)

    def to_live_data(self) -> LiveData:
        """Materialise the accumulated state into a LiveData (used by mqtt.state_messages_from_live).

        Fields that haven't been seen yet are filled with neutral defaults
        (zeros / `False`) so the LiveData shape stays consistent.
        """
        # Stitch cell voltages from the group dict (groups 0..3 → cells 1..16).
        cells: list[float] = []
        for g in (0, 1, 2, 3):
            cv = self.cell_voltages_by_group.get(g)
            if cv is not None:
                cells.extend(cv.voltages_v)
            else:
                cells.extend([0.0, 0.0, 0.0, 0.0])
        # Trim trailing zero-volt cells (those slots never sent → not real cells).
        while cells and cells[-1] == 0.0:
            cells.pop()
        if not cells:
            cells = [0.0]  # always have at least one cell for the LiveData invariant
        ms = self.main_status
        assert ms is not None  # guarded by has_enough_for_snapshot
        # Min/max from cell_minmax if present; otherwise compute from cells.
        if self.cell_minmax is not None:
            cmax = self.cell_minmax.cell_voltage_max_v
            cmin = self.cell_minmax.cell_voltage_min_v
            cmax_n = self.cell_minmax.cell_voltage_max_number
            cmin_n = self.cell_minmax.cell_voltage_min_number
            cdelta = self.cell_minmax.cell_voltage_delta_v
        else:
            cmax = max(cells)
            cmin = min(cells)
            cmax_n = cells.index(cmax) + 1
            cmin_n = cells.index(cmin) + 1
            cdelta = cmax - cmin
        cavg = sum(cells) / len(cells)

        # Temperatures: prefer individual probes, fall back to min/max if not seen.
        probe_1 = probe_2 = probe_3 = probe_4 = 0.0
        mos = 0.0
        if self.individual_temps is not None:
            temps = self.individual_temps.temperatures_c
            if len(temps) >= 1:
                mos = float(temps[0])
            if len(temps) >= 2:
                probe_1 = float(temps[1])
            if len(temps) >= 3:
                probe_2 = float(temps[2])
            if len(temps) >= 4:
                probe_3 = float(temps[3])
            if len(temps) >= 5:
                probe_4 = float(temps[4])
        elif self.temperatures is not None:
            mos = float(self.temperatures.avg_temp_c)
            probe_1 = float(self.temperatures.max_temp_c)
            probe_2 = float(self.temperatures.min_temp_c)

        # Power: from PowerCurrent, else compute from V × I.
        if self.power_current is not None:
            power = self.power_current.total_power_w
            current = self.power_current.total_current_a
            cycles = self.power_current.cycle_count
        else:
            current = ms.total_current_a
            power = ms.total_voltage_v * current
            cycles = 0

        return LiveData(
            cell_voltages_v=tuple(cells),
            cell_resistances_ohm=tuple([0.0] * len(cells)),
            cell_voltage_average_v=cavg,
            cell_voltage_delta_v=cdelta,
            cell_voltage_max_v=cmax,
            cell_voltage_min_v=cmin,
            cell_voltage_max_number=cmax_n,
            cell_voltage_min_number=cmin_n,
            mos_temp_c=mos,
            probe_1_temp_c=probe_1,
            probe_2_temp_c=probe_2,
            probe_3_temp_c=probe_3,
            probe_4_temp_c=probe_4,
            total_voltage_v=ms.total_voltage_v,
            total_current_a=current,
            total_power_w=power,
            balance_current_a=0.0,
            balance_action=0,
            soc_percentage=ms.soc_percentage,
            soh_percentage=100,
            remaining_capacity_ah=0.0,
            battery_capacity_ah=0.0,
            cycle_count=cycles,
            cycle_capacity_ah=0.0,
            total_runtime_s=0,
            switch_charge=True,
            switch_discharge=True,
            switch_balance=False,
            heating=False,
            heating_current_a=0.0,
            charge_status=0,
            charge_status_time_s=0,
        )


@dataclass
class CanRunner:
    """Receives JK CAN frames, snapshots a LiveData, publishes to MQTT.

    The CAN protocol is read-only on the JK BMS — writes never happen here.
    HA Discovery publishes only the read-only entities.
    """

    settings: Settings
    transport: CanBusTransport
    mqtt: object
    bms_name: str = "JK_BMS_1"
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S

    _accumulator: CanFrameAccumulator = field(default_factory=CanFrameAccumulator, init=False)
    _discovered: bool = field(default=False, init=False)

    async def run(self) -> None:
        recv_task = asyncio.create_task(self._recv_loop())
        flush_task = asyncio.create_task(self._flush_loop())
        try:
            await asyncio.gather(recv_task, flush_task)
        except asyncio.CancelledError:
            recv_task.cancel()
            flush_task.cancel()
            raise

    async def _recv_loop(self) -> None:
        while True:
            try:
                msg = await self.transport.recv_message(timeout_s=RECV_TIMEOUT_S)
            except ConnectionError as exc:
                logger.warning("CAN bus disconnected: %s — retrying", exc)
                await asyncio.sleep(1.0)
                continue
            if msg is None:
                continue
            try:
                self._accumulator.apply(msg.arbitration_id, msg.data)
            except ValueError as exc:
                logger.debug("CAN frame decode error for id=%#x: %s", msg.arbitration_id, exc)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_s)
            if not self._discovered:
                await self._announce_discovery()
            if self._accumulator.has_enough_for_snapshot():
                live = self._accumulator.to_live_data()
                await self._publish_live(live)

    async def _announce_discovery(self) -> None:
        messages = build_discovery_messages(
            settings=self.settings,
            bms_name=self.bms_name,
            cell_count=16,  # CAN doesn't report cell_count; assume 16
        )
        for msg in messages:
            topic, payload = render(msg)
            await self.mqtt.publish(topic, payload=payload, qos=1, retain=True)  # type: ignore[attr-defined]
        self._discovered = True

    async def _publish_live(self, live: LiveData) -> None:
        for topic, payload in state_messages_from_live(live, self.bms_name):
            await self.mqtt.publish(topic, payload=payload, qos=0)  # type: ignore[attr-defined]
