"""Tests for the SocketCAN transport using a fake python-can Bus factory."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from jkbms2mqtt.transport.can_bus import (
    CanBusTransport,
    CanMessage,
    _default_factory,
    connect_with_backoff,
)


@dataclass
class FakeCanMessage:
    arbitration_id: int
    data: bytes
    is_extended_id: bool = True


@dataclass
class FakeBus:
    """In-memory python-can substitute."""

    messages: list[FakeCanMessage]
    shutdowns: int = 0

    def recv(self, timeout: float) -> FakeCanMessage | None:
        del timeout
        if not self.messages:
            return None
        return self.messages.pop(0)

    def shutdown(self) -> None:
        self.shutdowns += 1


def _make_factory(messages: list[FakeCanMessage]) -> Any:
    instances: list[FakeBus] = []

    def _factory(**kwargs: object) -> FakeBus:
        bus = FakeBus(messages=list(messages))
        instances.append(bus)
        _factory.last_kwargs = kwargs  # type: ignore[attr-defined]
        _factory.last_bus = bus  # type: ignore[attr-defined]
        return bus

    _factory.instances = instances  # type: ignore[attr-defined]
    return _factory


async def test_connect_opens_bus() -> None:
    factory = _make_factory([])
    t = CanBusTransport(channel="can0")
    t._factory = factory
    await t.connect()
    assert t.is_connected
    assert factory.last_kwargs == {"channel": "can0", "interface": "socketcan"}
    await t.aclose()


async def test_connect_idempotent() -> None:
    factory = _make_factory([])
    t = CanBusTransport(channel="can0")
    t._factory = factory
    await t.connect()
    await t.connect()
    assert len(factory.instances) == 1
    await t.aclose()


async def test_recv_message_returns_canmessage() -> None:
    msg = FakeCanMessage(arbitration_id=0x2F4, data=bytes(8))
    factory = _make_factory([msg])
    t = CanBusTransport(channel="can0")
    t._factory = factory
    await t.connect()
    out = await t.recv_message(timeout_s=0.1)
    assert isinstance(out, CanMessage)
    assert out.arbitration_id == 0x2F4
    assert out.data == bytes(8)
    assert out.is_extended is True
    await t.aclose()


async def test_recv_message_returns_none_on_timeout() -> None:
    factory = _make_factory([])  # empty queue
    t = CanBusTransport(channel="can0")
    t._factory = factory
    await t.connect()
    out = await t.recv_message(timeout_s=0.1)
    assert out is None
    await t.aclose()


async def test_recv_message_without_connect_raises() -> None:
    t = CanBusTransport(channel="can0")
    with pytest.raises(ConnectionError):
        await t.recv_message(timeout_s=0.1)


async def test_aclose_idempotent() -> None:
    factory = _make_factory([])
    t = CanBusTransport(channel="can0")
    t._factory = factory
    await t.connect()
    await t.aclose()
    await t.aclose()
    assert not t.is_connected
    assert factory.last_bus.shutdowns == 1  # type: ignore[attr-defined]


async def test_connect_with_backoff_uses_label(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    import logging

    async def _no_sleep(_d: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    caplog.set_level(logging.WARNING)

    def _failing_factory(**_kwargs: object) -> FakeBus:
        raise OSError("no socketcan")

    t = CanBusTransport(channel="can0")
    t._factory = _failing_factory
    with pytest.raises(OSError):
        await connect_with_backoff(t, max_attempts=2)
    assert "CAN bus" in caplog.text


def test_default_factory_imports_or_raises() -> None:
    try:
        factory = _default_factory()
    except RuntimeError:
        pytest.skip("python-can not installed in this environment")
    assert callable(factory)
