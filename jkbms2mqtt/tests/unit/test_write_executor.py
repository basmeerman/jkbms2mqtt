"""Write-executor tests with a fake pymodbus client."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pytest

from jkbms2mqtt.config import Settings, Transport
from jkbms2mqtt.write_executor import (
    WriteExecutor,
    WriteRequest,
    parse_boolean_payload,
    parse_numeric_payload,
)

# -- Fake pymodbus client --------------------------------------------------------------


@dataclass
class FakeResponse:
    """Mimics pymodbus' response with isError()/registers attributes."""

    error: bool = False
    registers: list[int] = field(default_factory=list)

    def isError(self) -> bool:
        return self.error

    def __str__(self) -> str:
        return f"FakeResponse(error={self.error})"


@dataclass
class FakeClient:
    """Captures pymodbus calls; returns canned responses."""

    write_responses: deque[FakeResponse | Exception] = field(default_factory=deque)
    write_register_responses: deque[FakeResponse | Exception] = field(default_factory=deque)
    read_responses: deque[FakeResponse | Exception] = field(default_factory=deque)

    last_write_address: int | None = None
    last_write_values: list[int] | None = None
    last_write_register_value: int | None = None
    last_write_slave: int | None = None
    last_read_address: int | None = None

    async def write_registers(
        self, *, address: int, values: list[int], device_id: int
    ) -> Any:
        self.last_write_address = address
        self.last_write_values = list(values)
        self.last_write_slave = device_id
        if not self.write_responses:
            return FakeResponse(error=False)
        item = self.write_responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    async def write_register(self, *, address: int, value: int, device_id: int) -> Any:
        self.last_write_address = address
        self.last_write_register_value = value
        self.last_write_slave = device_id
        if not self.write_register_responses:
            return FakeResponse(error=False)
        item = self.write_register_responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    async def read_holding_registers(
        self, *, address: int, count: int, device_id: int
    ) -> Any:
        self.last_read_address = address
        if not self.read_responses:
            return FakeResponse(error=False, registers=[0] * count)
        item = self.read_responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class PublishLog:
    log: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, topic: str, payload: str) -> None:
        self.log.append((topic, payload))


def _settings(*, basic: bool = True, safety: bool = True) -> Settings:
    return Settings(
        transport=Transport.TCP_GATEWAY,
        gateway_host="x.x.x.x",
        gateway_port=502,
        enable_basic_writes=basic,
        enable_safety_writes=safety,
    )


# -- Payload-parsing helpers -----------------------------------------------------------


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


# -- Single-register writes ------------------------------------------------------------


async def test_basic_number_write_round_trip() -> None:
    """Round-trip a verified basic-tier numeric write to confirm address + encoding."""
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_voltage", raw_payload="3.5"
        )
    )
    assert client.last_write_address == 0x1000
    # U32_MILLI: 3.5 → 3500 → [0x0000, 0x0DAC]
    assert client.last_write_values == [0, 0x0DAC]
    assert client.last_write_slave == 1
    assert ("BMS_1/control/smart_sleep_voltage", "3.500") in pub.log


async def test_safety_number_write() -> None:
    """max_charge_current at the verified address 0x1016 with mA encoding."""
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=2, object_id="max_charge_current", raw_payload="40.0"
        )
    )
    assert client.last_write_address == 0x1016
    # U32_MILLI: 40.0 A → 40000 mA → [0x0000, 0x9C40]
    assert client.last_write_values == [0, 0x9C40]
    assert client.last_write_slave == 2
    assert ("BMS_1/control/max_charge_current", "40.000") in pub.log


async def test_basic_tier_disabled_refuses() -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(basic=False), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_voltage", raw_payload="3.5"
        )
    )
    assert client.last_write_address is None
    assert any(
        t == "BMS_1/error" and "enable_basic_writes" in json.loads(p)["reason"]
        for t, p in pub.log
    )


async def test_safety_tier_disabled_refuses() -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(safety=False), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="max_charge_current", raw_payload="50"
        )
    )
    assert client.last_write_address is None


async def test_out_of_range_rejected_pre_wire() -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="max_charge_current", raw_payload="700"
        )
    )
    assert client.last_write_address is None
    assert any("outside" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


async def test_garbage_numeric_payload_rejected() -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="max_charge_current", raw_payload="abc"
        )
    )
    assert client.last_write_address is None


async def test_garbage_boolean_payload_rejected() -> None:
    """Boolean parsing is exercised via a packed-bit entity (no BOOL32 regs remain)."""
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="kinda"
        )
    )
    assert client.last_write_address is None


async def test_modbus_exception_response_published_as_error() -> None:
    client = FakeClient(write_responses=deque([FakeResponse(error=True)]))
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_voltage", raw_payload="3.5"
        )
    )
    assert any("BMS rejected" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


async def test_connection_error_published_as_error() -> None:
    client = FakeClient(write_responses=deque([ConnectionError("dropped")]))
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_voltage", raw_payload="3.5"
        )
    )
    assert any("write failed" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


async def test_timeout_published_as_error() -> None:
    client = FakeClient(write_responses=deque([TimeoutError("slow")]))
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_voltage", raw_payload="3.5"
        )
    )
    assert any("write failed" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


async def test_unknown_object_id_rejected() -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="not_a_param", raw_payload="ON"
        )
    )
    assert any("unknown parameter" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


# -- Packed-bit writes ----------------------------------------------------------------


async def test_packed_bit_round_trip() -> None:
    client = FakeClient(read_responses=deque([FakeResponse(registers=[0x00])]))
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON"
        )
    )
    assert client.last_read_address == 0x1114
    assert client.last_write_address == 0x1114
    assert client.last_write_register_value == 0x40  # bit 6 set
    assert ("BMS_1/control/smart_sleep_switch", "ON") in pub.log


async def test_packed_bit_preserves_other_bits() -> None:
    # Current value has PCL bit on (0x80). Setting smart sleep → 0xC0.
    client = FakeClient(read_responses=deque([FakeResponse(registers=[0x80])]))
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON"
        )
    )
    assert client.last_write_register_value == 0xC0


async def test_packed_bit_tier_disabled() -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(basic=False), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON"
        )
    )
    assert client.last_read_address is None


async def test_packed_bit_garbage_payload() -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="nope"
        )
    )
    assert client.last_read_address is None


async def test_packed_bit_rmw_read_fails() -> None:
    client = FakeClient(read_responses=deque([ConnectionError("dropped")]))
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON"
        )
    )
    assert any(
        "read-modify-write read failed" in json.loads(p)["reason"]
        for t, p in pub.log if t.endswith("/error")
    )


async def test_packed_bit_rmw_read_returns_modbus_error() -> None:
    client = FakeClient(read_responses=deque([FakeResponse(error=True, registers=[0])]))
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON"
        )
    )
    assert any("RMW read" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


async def test_packed_bit_write_fails() -> None:
    client = FakeClient(
        read_responses=deque([FakeResponse(registers=[0])]),
        write_register_responses=deque([ConnectionError("nope")]),
    )
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON"
        )
    )
    assert any("write failed" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


async def test_packed_bit_write_returns_modbus_error() -> None:
    client = FakeClient(
        read_responses=deque([FakeResponse(registers=[0])]),
        write_register_responses=deque([FakeResponse(error=True)]),
    )
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_one(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_switch", raw_payload="ON"
        )
    )
    assert any("BMS rejected" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


# -- Run loop ---------------------------------------------------------------------------


async def test_run_drains_queue() -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    queue: asyncio.Queue[WriteRequest] = asyncio.Queue()
    await queue.put(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_voltage", raw_payload="3.5"
        )
    )
    task = asyncio.create_task(exec_.run(queue))
    await queue.join()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("BMS_1/control/smart_sleep_voltage", "3.500") in pub.log


async def test_run_recovers_from_handler_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    queue: asyncio.Queue[WriteRequest] = asyncio.Queue()

    async def boom(self, req: WriteRequest) -> None:
        raise RuntimeError("simulated handler crash")

    monkeypatch.setattr(WriteExecutor, "_handle_one", boom)
    await queue.put(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1, object_id="smart_sleep_voltage", raw_payload="3.5"
        )
    )
    task = asyncio.create_task(exec_.run(queue))
    for _ in range(200):
        if any("internal error" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error")):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any("internal error" in json.loads(p)["reason"] for t, p in pub.log if t.endswith("/error"))


# -- BOOL32 path with synthetic entity (no real BOOL32 register in the verified table) --


async def test_bool32_register_write_round_trip() -> None:
    """Exercise the BOOL32 encoding path on a synthetic WritableEntity.

    No verified writable currently uses BOOL32, but the encoder/handler support
    it for future re-additions (e.g. if a firmware variant exposes
    charging_switch at a known address). Confirms boolean payload parsing and
    "ON"/"OFF" state echo.
    """
    from jkbms2mqtt.entities import Component, WritableEntity
    from jkbms2mqtt.protocol.jk_settings import Encoding, RegisterDef, WriteTier

    reg = RegisterDef(
        name="synthetic_bool", address=0x1090, encoding=Encoding.BOOL32,
        min_value=0, max_value=1, step=1, unit=None,
        tier=WriteTier.BASIC, description="synthetic BOOL32 reg for tests",
    )
    entity = WritableEntity(
        object_id="synthetic_bool", topic_suffix="control/synthetic_bool",
        register=reg, component=Component.SWITCH, description="test",
    )

    client = FakeClient()
    pub = PublishLog()
    exec_ = WriteExecutor(client=client, settings=_settings(), publish=pub)  # type: ignore[arg-type]
    await exec_._handle_register(
        WriteRequest(
            bms_name="BMS_1", slave_addr=1,
            object_id="synthetic_bool", raw_payload="ON",
        ),
        entity,
    )
    # BOOL32 encodes True as [0, 1].
    assert client.last_write_address == 0x1090
    assert client.last_write_values == [0, 1]
    assert ("BMS_1/control/synthetic_bool", "ON") in pub.log
