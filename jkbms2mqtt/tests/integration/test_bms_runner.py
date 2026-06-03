"""End-to-end BmsRunner tests against a fake pymodbus client.

A real pymodbus.server would tie us to the library's internals; instead we
use a small fake client that returns canned register data. This still
exercises every code path in BmsRunner — the contract is the
read_holding_registers signature, which is pymodbus' own public API.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from jkbms2mqtt.bms_runner import (
    BASE_INFO,
    BLOCK_A_COUNT,
    BLOCK_B_COUNT,
    BLOCK_B_OFFSET,
    BLOCK_C_COUNT,
    BLOCK_C_OFFSET,
    BmsRunner,
)
from jkbms2mqtt.config import Settings, Transport
from jkbms2mqtt.protocol.jk_modbus import BASE_RT, INFO_BLOCK_WORDS
from jkbms2mqtt.protocol.jk_settings import (
    PACKED_BIT_REGISTER,
    SETTINGS_BLOCK_BASE,
    SETTINGS_BLOCK_CHUNKS,
    SETTINGS_BLOCK_WORDS,
)

# -- Fake client + helpers --------------------------------------------------------------


@dataclass
class FakeResponse:
    registers: list[int] = field(default_factory=list)
    error: bool = False

    def isError(self) -> bool:
        return self.error


@dataclass
class FakeClient:
    """Returns canned responses keyed on (slave_id, address) → list of regs."""

    map: dict[tuple[int, int], FakeResponse] = field(default_factory=dict)
    # If a key is not found, behaviour is controlled by:
    miss: Any = None  # FakeResponse, Exception, or None

    calls: list[tuple[int, int, int]] = field(default_factory=list)

    async def read_holding_registers(
        self, *, address: int, count: int, device_id: int
    ) -> Any:
        self.calls.append((device_id, address, count))
        key = (device_id, address)
        if key in self.map:
            r = self.map[key]
            return FakeResponse(
                registers=list(r.registers)[:count]
                + [0] * max(0, count - len(r.registers)),
                error=r.error,
            )
        if isinstance(self.miss, Exception):
            raise self.miss
        if isinstance(self.miss, FakeResponse):
            return self.miss
        return FakeResponse(error=True)


@dataclass
class PublishCapture:
    log: list[tuple[str, str, int, bool]] = field(default_factory=list)

    async def __call__(self, topic: str, payload: str, qos: int, retain: bool) -> None:
        self.log.append((topic, payload, qos, retain))


def _settings(**overrides: Any) -> Settings:
    base = {
        "transport": Transport.TCP_GATEWAY,
        "gateway_host": "x.x.x.x",
        "gateway_port": 502,
        "poll_interval_s": 1.0,
    }
    base.update(overrides)
    return Settings(**base)


def _block_a_for_pack_at(*, voltage_v: float, soc: int, cell_count: int = 16) -> list[int]:
    """Build a 120-register block A with cells, total V, total I, SoC etc."""
    regs = [0] * BLOCK_A_COUNT
    # Cell-present bitmap: cells 1..cell_count.
    mask = (1 << cell_count) - 1
    regs[0x20] = (mask >> 16) & 0xFFFF
    regs[0x21] = mask & 0xFFFF
    # Cell voltages: all at 3300 mV.
    for i in range(cell_count):
        regs[i] = 3300
    # MOSFET temp 25.0 °C
    regs[0x45] = 250
    # Total voltage (mV)
    mv = int(round(voltage_v * 1000))
    regs[0x48] = (mv >> 16) & 0xFFFF
    regs[0x49] = mv & 0xFFFF
    # Total current 0
    regs[0x4C] = 0
    regs[0x4D] = 0
    # Probe 1 / 2 temps 24.0 / 24.5
    regs[0x4E] = 240
    regs[0x4F] = 245
    # Balance state + SoC: balance=0, SoC=soc
    regs[0x53] = soc
    # Charge|discharge enabled
    regs[0x60] = (1 << 8) | 1
    return regs


def _info_block(*, model: str = "JK-PB2A16S15P", sw: str = "SW1209HE") -> list[int]:
    """Pack model/hw/sw/serial into an INFO block."""
    regs = [0] * INFO_BLOCK_WORDS

    def _pack(off: int, text: str, length_bytes: int) -> None:
        encoded = text.encode("ascii")[:length_bytes].ljust(length_bytes, b"\x00")
        for i in range(length_bytes // 2):
            regs[off + i] = (encoded[2 * i] << 8) | encoded[2 * i + 1]

    _pack(0x00, model, 16)
    _pack(0x08, "HW10A20H", 8)
    _pack(0x0C, sw, 8)
    _pack(0x28, "JK202401012345", 16)
    return regs


# -- Tests --------------------------------------------------------------------------


async def test_first_cycle_publishes_discovery_state_and_static_info() -> None:
    client = FakeClient(
        map={
            (1, BASE_RT): FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=75)),
            (1, BASE_RT + BLOCK_B_OFFSET): FakeResponse(registers=[0] * BLOCK_B_COUNT),
            (1, BASE_RT + BLOCK_C_OFFSET): FakeResponse(registers=[0] * BLOCK_C_COUNT),
            (1, BASE_INFO): FakeResponse(registers=_info_block()),
        }
    )
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner.announce_discovery()
    await runner._poll_once()

    topics = [t for t, _, _, _ in pub.log]
    # Discovery topic shape: homeassistant/sensor/BMS_1_device_<obj>/config
    assert any("BMS_1_device_total_voltage/config" in t for t in topics)
    # State
    assert ("BMS_1/Total_Voltage_V", "53.000", 0, False) in pub.log
    assert ("BMS_1/SOC_percentage", "75", 0, False) in pub.log
    # Static info
    assert ("BMS_1/bms", "JK-PB2A16S15P", 0, True) in pub.log


async def test_six_bms_each_publishes_independently() -> None:
    """Closes the 'only BMS_5 works' bug: 6 BMSes → 6 distinct device topics."""
    client = FakeClient(
        map={
            (sid, BASE_RT): FakeResponse(
                registers=_block_a_for_pack_at(voltage_v=53.0 + sid * 0.01, soc=70 + sid)
            )
            for sid in range(1, 7)
        }
    )
    # Block B / C / INFO miss → graceful (defaults to error response)
    pub = PublishCapture()
    runners = [
        BmsRunner(
            client=client,  # type: ignore[arg-type]
            settings=_settings(),
            slave_addr=sid,
            bms_name=f"BMS_{sid}",
            publish=pub,
        )
        for sid in range(1, 7)
    ]
    for r in runners:
        await r._poll_once()

    topics = {t for t, _, _, _ in pub.log}
    # Each BMS has its own Total_Voltage_V publish.
    for sid in range(1, 7):
        assert f"BMS_{sid}/Total_Voltage_V" in topics


async def test_block_b_failure_does_not_block_publish() -> None:
    client = FakeClient(
        map={
            (1, BASE_RT): FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50)),
            # Block B + C miss → FakeResponse(error=True)
        },
        miss=FakeResponse(error=True),
    )
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    assert any(t == "BMS_1/Total_Voltage_V" for t, _, _, _ in pub.log)


async def test_block_a_failure_skips_publish() -> None:
    client = FakeClient(map={}, miss=FakeResponse(error=True))
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    # Nothing published from state — but no crash either.
    state_topics = [t for t, _, q, _ in pub.log if q == 0]
    assert not any(t == "BMS_1/Total_Voltage_V" for t in state_topics)


async def test_block_a_connection_error_caught() -> None:
    client = FakeClient(map={}, miss=ConnectionError("dropped"))
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    # Connection error during block A → poll returns silently.


async def test_block_b_connection_error_does_not_crash() -> None:
    """Block B failure must not abort the cycle."""
    # Use a custom client that fails only on block B address.
    @dataclass
    class _Client:
        called: list[tuple[int, int]] = field(default_factory=list)

        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            self.called.append((device_id, address))
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            if address == BASE_RT + BLOCK_B_OFFSET:
                raise ConnectionError("block B dropped")
            if address == BASE_RT + BLOCK_C_OFFSET:
                return FakeResponse(registers=[0] * BLOCK_C_COUNT)
            return FakeResponse(error=True)

    client = _Client()
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    assert any(t == "BMS_1/Total_Voltage_V" for t, _, _, _ in pub.log)


async def test_block_c_connection_error_does_not_crash() -> None:
    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            if address == BASE_RT + BLOCK_C_OFFSET:
                raise ConnectionError("block C dropped")
            return FakeResponse(registers=[0] * 50)

    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=PublishCapture(),
    )
    await runner._poll_once()


async def test_static_info_read_error_does_not_block_state() -> None:
    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_INFO:
                raise TimeoutError("info read slow")
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            return FakeResponse(registers=[0] * count)

    pub = PublishCapture()
    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    # State published; static info did not crash.
    assert any(t == "BMS_1/Total_Voltage_V" for t, _, _, _ in pub.log)
    # No 'bms' static-info topic published.
    assert not any(t == "BMS_1/bms" for t, _, _, _ in pub.log)


async def test_static_info_modbus_error_skipped() -> None:
    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_INFO:
                return FakeResponse(error=True)
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            return FakeResponse(registers=[0] * count)

    pub = PublishCapture()
    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    assert not any(t == "BMS_1/bms" for t, _, _, _ in pub.log)


async def test_static_info_published_only_once() -> None:
    client = FakeClient(
        map={
            (1, BASE_RT): FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50)),
            (1, BASE_INFO): FakeResponse(registers=_info_block()),
        }
    )
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    await runner._poll_once()
    bms_topic_count = sum(1 for t, _, _, _ in pub.log if t == "BMS_1/bms")
    assert bms_topic_count == 1


async def test_announce_discovery_idempotent() -> None:
    client = FakeClient(map={})
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner.announce_discovery()
    first_count = len(pub.log)
    await runner.announce_discovery()
    assert len(pub.log) == first_count  # second call is a no-op


async def test_cell_count_change_republishes_discovery() -> None:
    """If the BMS reports a different cell count, discovery is re-emitted."""
    # First poll: 8 cells. Second poll: 16 cells.
    client = FakeClient(
        map={
            (1, BASE_RT): FakeResponse(
                registers=_block_a_for_pack_at(voltage_v=26.0, soc=50, cell_count=8)
            ),
        }
    )
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner.announce_discovery()  # initial: cell_count=16 default
    # Snapshot count of discovery topics so we can detect re-announce.
    discovery_count_before = sum(
        1 for t, _, _, r in pub.log if r and t.endswith("/config")
    )
    await runner._poll_once()  # detects cell_count=8 → re-announces
    discovery_count_after = sum(
        1 for t, _, _, r in pub.log if r and t.endswith("/config")
    )
    assert discovery_count_after > discovery_count_before


# -- poll_loop -------------------------------------------------------------------------


async def test_poll_loop_runs_then_cancels() -> None:
    """The poll_loop must publish at least once and cleanly accept cancellation."""
    client = FakeClient(
        map={
            (1, BASE_RT): FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50)),
        }
    )
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(poll_interval_s=1.0),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    task = asyncio.create_task(runner.poll_loop())
    for _ in range(100):
        if any(t == "BMS_1/Total_Voltage_V" for t, _, _, _ in pub.log):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(t == "BMS_1/Total_Voltage_V" for t, _, _, _ in pub.log)


# -- Settings readback -----------------------------------------------------------------


def _settings_block_with(*, max_charge_a: float = 80.0, charge_on: bool = True) -> list[int]:
    """Build a settings block with a couple of recognisable values set."""
    regs = [0] * SETTINGS_BLOCK_WORDS
    # max_charge_current is at 0x102C (encoding U32_DECI → value × 10).
    val = int(round(max_charge_a * 10))
    off = 0x102C - SETTINGS_BLOCK_BASE
    regs[off] = (val >> 16) & 0xFFFF
    regs[off + 1] = val & 0xFFFF
    # charging_switch at 0x1070 (BOOL32).
    off = 0x1070 - SETTINGS_BLOCK_BASE
    regs[off + 1] = 1 if charge_on else 0
    return regs


def _settings_chunk_map(slave: int, block: list[int]) -> dict[tuple[int, int], FakeResponse]:
    """Slice a full settings block into FakeResponses keyed by chunk address."""
    out: dict[tuple[int, int], FakeResponse] = {}
    for chunk_addr, chunk_count in SETTINGS_BLOCK_CHUNKS:
        off = chunk_addr - SETTINGS_BLOCK_BASE
        out[(slave, chunk_addr)] = FakeResponse(registers=block[off : off + chunk_count])
    return out


async def test_settings_block_state_published() -> None:
    block = _settings_block_with(max_charge_a=80.0, charge_on=True)
    client = FakeClient(
        map={
            (1, BASE_RT): FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50)),
            (1, PACKED_BIT_REGISTER): FakeResponse(registers=[0x0040]),  # smart_sleep on
            **_settings_chunk_map(1, block),
        }
    )
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()

    by_topic = {t: p for t, p, _, _ in pub.log}
    # Settings state is published whether or not the tier is enabled.
    assert by_topic.get("BMS_1/control/max_charge_current") == "80.0"
    assert by_topic.get("BMS_1/control/charging_switch") == "ON"
    assert by_topic.get("BMS_1/control/smart_sleep_switch") == "ON"


def _settings_chunk_addrs() -> tuple[int, ...]:
    return tuple(addr for addr, _ in SETTINGS_BLOCK_CHUNKS)


async def test_settings_block_modbus_error_skipped(caplog: pytest.LogCaptureFixture) -> None:
    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            if address in _settings_chunk_addrs():
                return FakeResponse(error=True)
            return FakeResponse(registers=[0] * count)

    pub = PublishCapture()
    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    with caplog.at_level("WARNING"):
        await runner._poll_once()
    # State (block A) still publishes; settings topic does not.
    assert not any(
        t == "BMS_1/control/max_charge_current" for t, _, _, _ in pub.log
    )
    # First-time failure surfaces at WARNING so the user can see it.
    assert any("settings readback failed" in rec.message for rec in caplog.records)


async def test_settings_block_timeout_skipped() -> None:
    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            if address in _settings_chunk_addrs():
                raise TimeoutError("settings slow")
            return FakeResponse(registers=[0] * count)

    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=PublishCapture(),
    )
    await runner._poll_once()


async def test_one_settings_chunk_failure_still_publishes_other_chunk() -> None:
    """Partial chunk failure must not block publishing settings from the chunk that succeeded."""
    # Place balance_trigger_voltage (0x1014, U32_MILLI, 0.003..1.000) in the
    # block. It lives in chunk 1 (0x1000..0x1063) so it survives a chunk-2 failure.
    block = [0] * SETTINGS_BLOCK_WORDS
    btv_addr = 0x1014  # balance_trigger_voltage
    raw = int(round(0.005 * 1000))  # 5 mV → matches U32_MILLI scaling
    off = btv_addr - SETTINGS_BLOCK_BASE
    block[off] = (raw >> 16) & 0xFFFF
    block[off + 1] = raw & 0xFFFF

    chunk_addrs = _settings_chunk_addrs()
    first_chunk_addr = chunk_addrs[0]
    second_chunk_addr = chunk_addrs[1]

    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            if address == first_chunk_addr:
                off = first_chunk_addr - SETTINGS_BLOCK_BASE
                return FakeResponse(registers=block[off : off + count])
            if address == second_chunk_addr:
                return FakeResponse(error=True)
            return FakeResponse(registers=[0] * count)

    pub = PublishCapture()
    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    by_topic = {t: p for t, p, _, _ in pub.log}
    assert by_topic.get("BMS_1/control/balance_trigger_voltage") == "0.005"


async def test_settings_first_log_only_emits_once(caplog: pytest.LogCaptureFixture) -> None:
    """The introductory INFO line should fire once per BMS, not every cycle."""
    block = _settings_block_with()
    client = FakeClient(
        map={
            (1, BASE_RT): FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50)),
            (1, PACKED_BIT_REGISTER): FakeResponse(registers=[0x0000]),
            **_settings_chunk_map(1, block),
        }
    )
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    with caplog.at_level("INFO"):
        await runner._poll_once()
        await runner._poll_once()
    info_lines = [r for r in caplog.records if "settings readback OK" in r.message]
    assert len(info_lines) == 1


async def test_settings_failure_log_only_once_per_bms(caplog: pytest.LogCaptureFixture) -> None:
    """A persistent settings failure should emit one WARNING, not one per cycle."""
    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            if address in _settings_chunk_addrs():
                return FakeResponse(error=True)
            return FakeResponse(registers=[0] * count)

    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=PublishCapture(),
    )
    with caplog.at_level("WARNING"):
        await runner._poll_once()
        await runner._poll_once()
        await runner._poll_once()
    warn_lines = [r for r in caplog.records if "settings readback failed" in r.message]
    assert len(warn_lines) == 1


async def test_packed_bit_register_read_failure_skipped() -> None:
    block = _settings_block_with(max_charge_a=50.0, charge_on=False)

    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            if address in _settings_chunk_addrs():
                off = address - SETTINGS_BLOCK_BASE
                return FakeResponse(registers=block[off : off + count])
            if address == PACKED_BIT_REGISTER:
                raise ConnectionError("packed bit lost")
            return FakeResponse(registers=[0] * count)

    pub = PublishCapture()
    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    # Numeric settings still publish; packed bits skipped silently.
    assert any(
        t == "BMS_1/control/max_charge_current" for t, _, _, _ in pub.log
    )
    assert not any(
        t == "BMS_1/control/smart_sleep_switch" for t, _, _, _ in pub.log
    )


async def test_packed_bit_register_modbus_error_skipped() -> None:
    block = _settings_block_with(max_charge_a=50.0, charge_on=False)

    @dataclass
    class _Client:
        async def read_holding_registers(
            self, *, address: int, count: int, device_id: int
        ) -> Any:
            if address == BASE_RT:
                return FakeResponse(registers=_block_a_for_pack_at(voltage_v=53.0, soc=50))
            if address in _settings_chunk_addrs():
                off = address - SETTINGS_BLOCK_BASE
                return FakeResponse(registers=block[off : off + count])
            if address == PACKED_BIT_REGISTER:
                return FakeResponse(error=True)
            return FakeResponse(registers=[0] * count)

    pub = PublishCapture()
    runner = BmsRunner(
        client=_Client(),  # type: ignore[arg-type]
        settings=_settings(),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    await runner._poll_once()
    assert not any(
        t == "BMS_1/control/smart_sleep_switch" for t, _, _, _ in pub.log
    )


async def test_poll_loop_keeps_running_on_block_a_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConnectionError during a poll must not exit the loop."""
    client = FakeClient(map={}, miss=ConnectionError("dropped"))
    pub = PublishCapture()
    runner = BmsRunner(
        client=client,  # type: ignore[arg-type]
        settings=_settings(poll_interval_s=1.0),
        slave_addr=1,
        bms_name="BMS_1",
        publish=pub,
    )
    real_sleep = asyncio.sleep

    async def fast_sleep(d: float) -> None:
        if d >= 0.5:
            await real_sleep(0)
        else:
            await real_sleep(d)

    monkeypatch.setattr("jkbms2mqtt.bms_runner.asyncio.sleep", fast_sleep)

    task = asyncio.create_task(runner.poll_loop())
    await real_sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
