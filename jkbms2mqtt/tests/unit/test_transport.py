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
        assert isinstance(c, AsyncModbusTcpClient)
        assert c.comm_params.host == "10.0.0.1"
        assert c.comm_params.port == 502

    async def test_usb_serial(self) -> None:
        s = Settings(
            transport=Transport.USB_SERIAL,
            jkbms_path="/dev/ttyUSB0",
        )
        c = build_client(s)
        assert isinstance(c, AsyncModbusSerialClient)
        assert c.comm_params.host == "/dev/ttyUSB0"
        assert c.comm_params.baudrate == JK_BAUD_RATE


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
