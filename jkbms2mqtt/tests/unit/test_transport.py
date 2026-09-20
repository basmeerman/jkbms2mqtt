"""Tests for the pymodbus client factory + connect_with_backoff helper."""

from __future__ import annotations

import asyncio

import pytest
from pymodbus.client import AsyncModbusSerialClient, AsyncModbusTcpClient

from jkbms2mqtt.config import Settings, Transport
from jkbms2mqtt.transport import (
    INITIAL_BACKOFF_S,
    JK_BAUD_RATE,
    MAX_BACKOFF_S,
    FrameGapClient,
    build_client,
    connect_with_backoff,
)

# -- build_client ------------------------------------------------------------------------


class TestBuildClient:
    async def test_tcp_gateway(self) -> None:
        s = Settings(
            transport=Transport.TCP_GATEWAY,
            gateway_host="10.0.0.1",
            gateway_port=502,
        )
        c = build_client(s)
        assert isinstance(c, FrameGapClient)
        inner = c.inner
        assert isinstance(inner, AsyncModbusTcpClient)
        assert inner.comm_params.host == "10.0.0.1"
        assert inner.comm_params.port == 502

    async def test_usb_serial(self) -> None:
        s = Settings(
            transport=Transport.USB_SERIAL,
            jkbms_path="/dev/ttyUSB0",
        )
        c = build_client(s)
        assert isinstance(c, FrameGapClient)
        inner = c.inner
        assert isinstance(inner, AsyncModbusSerialClient)
        assert inner.comm_params.host == "/dev/ttyUSB0"
        assert inner.comm_params.baudrate == JK_BAUD_RATE

    async def test_gap_comes_from_settings(self) -> None:
        s = Settings(transport=Transport.TCP_GATEWAY, gateway_host="10.0.0.1",
                     min_frame_gap_ms=35)
        c = build_client(s)
        assert isinstance(c, FrameGapClient)
        assert c._min_gap_s == pytest.approx(0.035)


# -- FrameGapClient ----------------------------------------------------------------------


class _RecordingClient:
    """Stands in for pymodbus; records calls and can raise on demand."""

    def __init__(self, *, boom: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.boom = boom
        self.closed = False
        self.connected = False

    async def _record(self, name: str, **kw: object) -> str:
        self.calls.append((name, dict(kw)))
        if self.boom is not None:
            raise self.boom
        return f"{name}-response"

    async def read_holding_registers(self, **kw: object) -> str:
        return await self._record("read", **kw)

    async def write_registers(self, **kw: object) -> str:
        return await self._record("write_registers", **kw)

    async def write_register(self, **kw: object) -> str:
        return await self._record("write_register", **kw)

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.closed = True


class _Clock:
    """Deterministic monotonic clock that only advances when slept."""

    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        self.t += d


def _gapped(inner: object, gap: float = 0.05) -> tuple[FrameGapClient, _Clock]:
    clk = _Clock()
    c = FrameGapClient(
        inner,  # type: ignore[arg-type]
        min_gap_s=gap, sleeper=clk.sleep, clock=clk.now,
    )
    return c, clk


class TestFrameGapClient:
    async def test_first_transaction_delegates_without_waiting(self) -> None:
        """The bus has been idle since process start, so no gap is owed."""
        inner = _RecordingClient()
        c, clk = _gapped(inner)
        result = await c.read_holding_registers(address=0x1000, count=2, device_id=1)
        assert result == "read-response"
        assert inner.calls == [("read", {"address": 0x1000, "count": 2, "device_id": 1})]
        assert clk.sleeps == []

    async def test_back_to_back_calls_are_spaced(self) -> None:
        inner = _RecordingClient()
        c, clk = _gapped(inner, gap=0.05)
        await c.read_holding_registers(address=1, count=1, device_id=1)
        clk.sleeps.clear()
        await c.read_holding_registers(address=2, count=1, device_id=1)
        # No time passed between them, so the whole gap must be slept.
        assert clk.sleeps == [pytest.approx(0.05)]

    async def test_no_sleep_when_the_bus_was_already_idle(self) -> None:
        inner = _RecordingClient()
        c, clk = _gapped(inner, gap=0.05)
        await c.read_holding_registers(address=1, count=1, device_id=1)
        clk.t += 1.0          # a long quiet period
        clk.sleeps.clear()
        await c.read_holding_registers(address=2, count=1, device_id=1)
        assert clk.sleeps == []

    async def test_gap_is_measured_after_a_failed_transaction_too(self) -> None:
        """The finally clause must stamp _last_frame even when the call raises."""
        inner = _RecordingClient(boom=TimeoutError("no reply"))
        c, clk = _gapped(inner, gap=0.05)
        with pytest.raises(TimeoutError):
            await c.read_holding_registers(address=1, count=1, device_id=1)
        clk.sleeps.clear()
        with pytest.raises(TimeoutError):
            await c.read_holding_registers(address=2, count=1, device_id=1)
        assert clk.sleeps == [pytest.approx(0.05)]

    async def test_write_paths_delegate(self) -> None:
        inner = _RecordingClient()
        c, _ = _gapped(inner)
        assert await c.write_registers(address=0x1020, values=[0, 1], device_id=2) == (
            "write_registers-response"
        )
        assert await c.write_register(address=0x1114, value=0x40, device_id=3) == (
            "write_register-response"
        )
        assert [n for n, _ in inner.calls] == ["write_registers", "write_register"]
        assert inner.calls[0][1] == {"address": 0x1020, "values": [0, 1], "device_id": 2}
        assert inner.calls[1][1] == {"address": 0x1114, "value": 0x40, "device_id": 3}

    async def test_connect_and_close_delegate(self) -> None:
        inner = _RecordingClient()
        c, _ = _gapped(inner)
        assert await c.connect() is True
        assert inner.connected is True
        c.close()
        assert inner.closed is True

    async def test_concurrent_callers_are_serialised(self) -> None:
        """Six runners share one client; the lock is what makes spacing mean anything."""
        inner = _RecordingClient()
        c, clk = _gapped(inner, gap=0.01)
        await asyncio.gather(*(
            c.read_holding_registers(address=i, count=1, device_id=i) for i in range(1, 7)
        ))
        assert len(inner.calls) == 6
        # The first call owes nothing; each of the other five waits out the gap.
        assert clk.sleeps == [pytest.approx(0.01)] * 5


# -- connect_with_backoff ----------------------------------------------------------------


class _FlakyClient:
    """Test double mimicking a pymodbus client's ``connect()`` behaviour."""

    def __init__(self, fail_n_times: int, *, raise_oserror: bool = False) -> None:
        self.attempts = 0
        self.fail_n_times = fail_n_times
        self.raise_oserror = raise_oserror

    async def connect(self) -> bool:
        self.attempts += 1
        if self.attempts <= self.fail_n_times:
            if self.raise_oserror:
                raise OSError("simulated")
            return False
        return True


async def _no_sleep(_d: float) -> None:
    return None


async def test_succeeds_on_first_attempt() -> None:
    c = _FlakyClient(fail_n_times=0)
    await connect_with_backoff(c, sleeper=_no_sleep)  # type: ignore[arg-type]
    assert c.attempts == 1


async def test_succeeds_after_two_returned_false() -> None:
    c = _FlakyClient(fail_n_times=2)
    await connect_with_backoff(c, sleeper=_no_sleep)  # type: ignore[arg-type]
    assert c.attempts == 3


async def test_succeeds_after_oserror_then_ok() -> None:
    c = _FlakyClient(fail_n_times=1, raise_oserror=True)
    await connect_with_backoff(c, sleeper=_no_sleep)  # type: ignore[arg-type]
    assert c.attempts == 2


async def test_max_attempts_returned_false_raises_connection_error() -> None:
    c = _FlakyClient(fail_n_times=10)
    with pytest.raises(ConnectionError, match="3 attempts"):
        await connect_with_backoff(
            c, max_attempts=3, sleeper=_no_sleep  # type: ignore[arg-type]
        )
    assert c.attempts == 3


async def test_max_attempts_oserror_raises_connection_error() -> None:
    c = _FlakyClient(fail_n_times=10, raise_oserror=True)
    with pytest.raises(ConnectionError, match="simulated"):
        await connect_with_backoff(
            c, max_attempts=2, sleeper=_no_sleep  # type: ignore[arg-type]
        )


async def test_backoff_doubles_and_caps() -> None:
    """Verify the backoff schedule: 1, 2, 4, 8, 16, 30, 30 (caps at MAX)."""
    sleeps: list[float] = []

    async def record(d: float) -> None:
        sleeps.append(d)

    c = _FlakyClient(fail_n_times=10, raise_oserror=True)
    with pytest.raises(ConnectionError):
        await connect_with_backoff(
            c, max_attempts=8, sleeper=record  # type: ignore[arg-type]
        )
    assert sleeps == [
        INITIAL_BACKOFF_S,
        INITIAL_BACKOFF_S * 2,
        INITIAL_BACKOFF_S * 4,
        INITIAL_BACKOFF_S * 8,
        INITIAL_BACKOFF_S * 16,
        MAX_BACKOFF_S,
        MAX_BACKOFF_S,
    ]


async def test_default_sleeper_is_asyncio_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect_with_backoff with no sleeper uses asyncio.sleep — patch it to no-op."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    try:
        c = _FlakyClient(fail_n_times=1)
        await connect_with_backoff(c)  # type: ignore[arg-type]
        assert c.attempts == 2
    finally:
        monkeypatch.setattr(asyncio, "sleep", real_sleep)
