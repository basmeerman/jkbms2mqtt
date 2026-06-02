"""Write-executor tests: ack parsing, mismatch handling, tier gating, and the
packed-bit read-modify-write pattern at register 0x1114."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

import pytest

from jkbms2mqtt.bus_arbiter import BusArbiter
from jkbms2mqtt.config import Settings
from jkbms2mqtt.protocol.capabilities import Topology, Transport
from jkbms2mqtt.protocol.decoder import SetupData
from jkbms2mqtt.protocol.modbus import (
    EXCEPTION_FLAG,
    FUNC_WRITE_MULTIPLE,
    FUNC_WRITE_SINGLE,
    append_crc,
)
from jkbms2mqtt.write_executor import (
    WriteExecutor,
    WriteRequest,
    parse_boolean_payload,
    parse_numeric_payload,
)


@dataclass
class FakeTransport:
    """A tiny transport that records writes and returns canned responses."""

    rx_queue: deque[bytes] = field(default_factory=deque)
    tx_log: list[bytes] = field(default_factory=list)
    _connected: bool = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def aclose(self) -> None:
        self._connected = False

    async def write(self, data: bytes) -> None:
        self.tx_log.append(data)

    # The executor reads in two chunks (header then body), so we maintain a
    # single byte buffer and slice it lazily.
    _rx_buffer: bytes = b""

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:
        del timeout_s
        while len(self._rx_buffer) < n:
            if not self.rx_queue:
                raise TimeoutError("no reply queued")
            self._rx_buffer += self.rx_queue.popleft()
        out = self._rx_buffer[:n]
        self._rx_buffer = self._rx_buffer[n:]
        return out


def _settings(*, basic: bool = True, safety: bool = True) -> Settings:
    return Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        topology=Topology.MASTER_POLL,
        enable_basic_writes=basic,
        enable_safety_writes=safety,
        inter_frame_gap_ms=10,
    )


@dataclass
class PublishLog:
    """Tiny in-memory publisher to record `(topic, payload)` tuples."""

    log: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, topic: str, payload: str) -> None:
        self.log.append((topic, payload))


# ---------------- parsing helpers ----------------


def test_parse_boolean_payload_variants() -> None:
    for s in ("on", "ON", "1", "true", "TRUE"):
        assert parse_boolean_payload(s) is True
    for s in ("off", "OFF", "0", "false", "FALSE"):
        assert parse_boolean_payload(s) is False


def test_parse_boolean_payload_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="boolean"):
        parse_boolean_payload("maybe")


def test_parse_numeric_payload() -> None:
    assert parse_numeric_payload("3.14") == pytest.approx(3.14)


# ---------------- single-register writes ----------------


def _make_ack_for(register: int, *, slave: int = 1) -> bytes:
    body = bytes([slave, FUNC_WRITE_MULTIPLE, (register >> 8) & 0xFF, register & 0xFF, 0x00, 0x02])
    return append_crc(body)


def _make_single_ack(register: int, value: int, *, slave: int = 1) -> bytes:
    body = bytes(
        [slave, FUNC_WRITE_SINGLE, (register >> 8) & 0xFF, register & 0xFF, (value >> 8) & 0xFF, value & 0xFF]
    )
    return append_crc(body)


def _exception_response(register: int, *, slave: int = 1, code: int = 0x02) -> bytes:
    body = bytes([slave, FUNC_WRITE_MULTIPLE | EXCEPTION_FLAG, code])
    return append_crc(body)


async def test_basic_switch_write_round_trip() -> None:
    """Posting `charging_switch=ON` produces a Modbus write; the ack updates the state topic."""
    transport = FakeTransport(rx_queue=deque([_make_ack_for(0x1070)]))
    arbiter = BusArbiter(inter_frame_gap_ms=0)
    pub = PublishLog()
    exec_ = WriteExecutor(transport=transport, arbiter=arbiter, settings=_settings(), publish=pub)
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1",
            slave_addr=1,
            object_id="charging_switch",
            raw_payload="ON",
        )
    )
    # Sent the correct Modbus frame
    assert len(transport.tx_log) == 1
    frame = transport.tx_log[0]
    assert frame[2:4] == bytes([0x10, 0x70])  # register 0x1070
    assert frame[7:11] == b"\x00\x00\x00\x01"  # BOOL32 ON
    # State topic updated
    assert ("BMS_1/control/charging_switch", "ON") in pub.log


async def test_safety_number_write() -> None:
    transport = FakeTransport(rx_queue=deque([_make_ack_for(0x102C)]))
    arbiter = BusArbiter(inter_frame_gap_ms=0)
    pub = PublishLog()
    exec_ = WriteExecutor(transport=transport, arbiter=arbiter, settings=_settings(), publish=pub)
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1",
            slave_addr=1,
            object_id="max_charge_current",
            raw_payload="80.0",
        )
    )
    frame = transport.tx_log[0]
    # 80.0 A → 800 deci-A → 0x00000320
    assert frame[7:11] == b"\x00\x00\x03\x20"
    assert ("BMS_1/control/max_charge_current", "80.000") in pub.log


async def test_basic_tier_disabled_refuses_write() -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport,
        arbiter=BusArbiter(inter_frame_gap_ms=0),
        settings=_settings(basic=False),
        publish=pub,
    )
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="charging_switch", raw_payload="ON"
        )
    )
    # No bytes sent.
    assert transport.tx_log == []
    # Error message published.
    assert any(t == "BMS_1/error" and "tier basic writes disabled" in p for t, p in pub.log)


async def test_safety_tier_disabled_refuses_write() -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport,
        arbiter=BusArbiter(inter_frame_gap_ms=0),
        settings=_settings(safety=False),
        publish=pub,
    )
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="max_charge_current", raw_payload="50"
        )
    )
    assert transport.tx_log == []
    assert any("tier safety writes disabled" in p for _, p in pub.log)


async def test_out_of_range_value_rejected() -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="max_charge_current", raw_payload="700"
        )
    )
    assert transport.tx_log == []
    assert any("outside" in p for _, p in pub.log)


async def test_garbage_numeric_payload_rejected() -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="max_charge_current", raw_payload="nope")
    )
    assert transport.tx_log == []
    assert any("nope" in p or "could not convert" in p.lower() for _, p in pub.log)


async def test_garbage_boolean_payload_rejected() -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="balance_switch", raw_payload="kinda")
    )
    assert transport.tx_log == []


async def test_modbus_exception_published_to_error_topic() -> None:
    transport = FakeTransport(rx_queue=deque([_exception_response(0x1070, code=0x02)]))
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="charging_switch", raw_payload="ON")
    )
    assert any("illegal data address" in p for _, p in pub.log)


async def test_malformed_ack_published_to_error_topic() -> None:
    # Send back a 6-byte garbage reply that fails CRC verification.
    transport = FakeTransport(rx_queue=deque([b"\x00" * 8]))
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="charging_switch", raw_payload="ON")
    )
    assert any("malformed ack" in p for _, p in pub.log)


async def test_ack_timeout_published_to_error_topic() -> None:
    transport = FakeTransport(rx_queue=deque())  # nothing queued → TimeoutError
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="charging_switch", raw_payload="ON")
    )
    assert any("timeout" in p for _, p in pub.log)


async def test_unknown_object_id_rejected() -> None:
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=FakeTransport(),
        arbiter=BusArbiter(inter_frame_gap_ms=0),
        settings=_settings(),
        publish=pub,
    )
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="totally_unknown", raw_payload="ON")
    )
    assert any("unknown parameter" in p for _, p in pub.log)


# ---------------- packed-bit writes ----------------


def _empty_setup() -> SetupData:
    """Hand-crafted SetupData with all bits cleared — used by packed-bit tests."""
    return SetupData(
        smart_sleep_voltage_v=0,
        cell_voltage_undervoltage_protection_v=0,
        cell_voltage_undervoltage_recovery_v=0,
        cell_voltage_overvoltage_protection_v=0,
        cell_voltage_overvoltage_recovery_v=0,
        balance_trigger_voltage_v=0,
        cell_soc100_voltage_v=0,
        cell_soc0_voltage_v=0,
        cell_request_charge_voltage_v=0,
        cell_request_float_voltage_v=0,
        power_off_voltage_v=0,
        max_charge_current_a=0,
        charge_overcurrent_protection_delay_s=0,
        charge_overcurrent_protection_recovery_time_s=0,
        max_discharge_current_a=0,
        discharge_overcurrent_protection_delay_s=0,
        discharge_overcurrent_protection_recovery_time_s=0,
        short_circuit_protection_recovery_time_s=0,
        max_balance_current_a=0,
        charge_overtemperature_protection_c=0,
        charge_overtemperature_protection_recovery_c=0,
        discharge_overtemperature_protection_c=0,
        discharge_overtemperature_protection_recovery_c=0,
        charge_undertemperature_protection_c=0,
        charge_undertemperature_protection_recovery_c=0,
        power_tube_overtemperature_protection_c=0,
        power_tube_overtemperature_protection_recovery_c=0,
        cell_count=16,
        charging_switch=False,
        discharging_switch=False,
        balance_switch=False,
        total_battery_capacity_ah=0,
        short_circuit_protection_delay_s=0,
        balance_starting_voltage_v=0,
        connection_wire_resistance_1_ohm=0,
        device_address=1,
        display_always_on_switch=False,
        smart_sleep_switch=False,
        disable_pcl_module_switch=False,
        timed_stored_data_switch=False,
    )


async def test_packed_bit_smart_sleep_on_with_no_setup_fails() -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON")
    )
    assert transport.tx_log == []
    assert any("no setup frame" in p for _, p in pub.log)


async def test_packed_bit_smart_sleep_on_round_trip() -> None:
    transport = FakeTransport(rx_queue=deque([_make_single_ack(0x1114, 0x40)]))
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    exec_.latest_setup = _empty_setup()
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON")
    )
    frame = transport.tx_log[0]
    # Function 0x06, register 0x1114, value 0x0040 (bit 6 set)
    assert frame[1] == 0x06
    assert frame[2:4] == bytes([0x11, 0x14])
    assert frame[4:6] == bytes([0x00, 0x40])
    assert ("BMS_1/control/smart_sleep_switch", "ON") in pub.log


async def test_packed_bit_preserves_other_bits() -> None:
    """If PCL is already on (bit 7), turning smart sleep on adds bit 6 → 0xC0."""
    transport = FakeTransport(rx_queue=deque([_make_single_ack(0x1114, 0xC0)]))
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    setup = _empty_setup()
    # Hack: SetupData is frozen, so build a new one
    import dataclasses

    exec_.latest_setup = dataclasses.replace(setup, disable_pcl_module_switch=True)
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON")
    )
    frame = transport.tx_log[0]
    assert frame[4:6] == bytes([0x00, 0xC0])


async def test_packed_bit_tier_disabled() -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport,
        arbiter=BusArbiter(inter_frame_gap_ms=0),
        settings=_settings(basic=False),
        publish=pub,
    )
    exec_.latest_setup = _empty_setup()
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON")
    )
    assert transport.tx_log == []


async def test_packed_bit_garbage_payload() -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    exec_.latest_setup = _empty_setup()
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="nope")
    )
    assert transport.tx_log == []


async def test_packed_bit_modbus_exception() -> None:
    transport = FakeTransport(
        rx_queue=deque(
            [append_crc(bytes([0x01, FUNC_WRITE_SINGLE | EXCEPTION_FLAG, 0x02]))]
        )
    )
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    exec_.latest_setup = _empty_setup()
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON")
    )
    assert any("Modbus exception" in p for _, p in pub.log)


async def test_packed_bit_timeout() -> None:
    transport = FakeTransport(rx_queue=deque())
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    exec_.latest_setup = _empty_setup()
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON")
    )
    assert any("timeout" in p for _, p in pub.log)


async def test_packed_bit_malformed_ack() -> None:
    transport = FakeTransport(rx_queue=deque([b"\x00" * 8]))
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    exec_.latest_setup = _empty_setup()
    await exec_._handle_one(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON")
    )
    assert any("malformed ack" in p for _, p in pub.log)


# ---------------- run loop ----------------


async def test_run_loop_drains_queue_and_recovers_from_handler_errors() -> None:
    transport = FakeTransport(rx_queue=deque([_make_ack_for(0x1070)]))
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    queue: asyncio.Queue[WriteRequest] = asyncio.Queue()
    await queue.put(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="charging_switch", raw_payload="ON")
    )
    task = asyncio.create_task(exec_.run(queue))
    await queue.join()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(t == "BMS_1/control/charging_switch" for t, _ in pub.log)


async def test_run_loop_handles_unexpected_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FakeTransport()
    pub = PublishLog()
    exec_ = WriteExecutor(
        transport=transport, arbiter=BusArbiter(inter_frame_gap_ms=0), settings=_settings(), publish=pub
    )
    queue: asyncio.Queue[WriteRequest] = asyncio.Queue()

    async def boom(self, req: WriteRequest) -> None:
        raise RuntimeError("simulated handler crash")

    monkeypatch.setattr(WriteExecutor, "_handle_one", boom)
    await queue.put(
        WriteRequest(bms_name="BMS_1", slave_addr=1, object_id="charging_switch", raw_payload="ON")
    )
    task = asyncio.create_task(exec_.run(queue))
    # Poll until the error is published (more robust than queue.join() under coverage).
    for _ in range(100):
        if any("internal error" in p for _, p in pub.log):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any("internal error" in p for _, p in pub.log)
