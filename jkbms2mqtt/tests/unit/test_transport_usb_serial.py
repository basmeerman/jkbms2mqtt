"""Tests for the USB serial transport with an injected fake opener."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from jkbms2mqtt.transport.usb_serial import (
    BAUD_RATE,
    UsbSerialTransport,
    _default_opener,
    connect_with_backoff,
)


def _make_fake_opener(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> Callable[..., Any]:
    async def _opener(**kwargs: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Record args on the function object so tests can inspect them.
        _opener.last_kwargs = kwargs  # type: ignore[attr-defined]
        return reader, writer

    return _opener


async def _build_in_memory_pair() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Spin up a tiny asyncio TCP loopback to obtain real StreamReader/Writer instances."""
    server_writes: list[bytes] = []

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            while True:
                d = await r.read(4096)
                if not d:
                    return
                server_writes.append(d)
                w.write(b"E:" + d)
                await w.drain()
        finally:
            w.close()

    server = await asyncio.start_server(handler, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    # Hide the server so we can close it later.
    writer._jkbms_server = server  # type: ignore[attr-defined]
    return reader, writer


async def test_connect_uses_baud_115200_by_default() -> None:
    reader, writer = await _build_in_memory_pair()
    try:
        opener = _make_fake_opener(reader, writer)
        t = UsbSerialTransport(device_path="/dev/ttyTEST")
        t._opener = opener
        await t.connect()
        assert opener.last_kwargs["baudrate"] == BAUD_RATE  # type: ignore[attr-defined]
        assert opener.last_kwargs["url"] == "/dev/ttyTEST"  # type: ignore[attr-defined]
        assert t.is_connected
    finally:
        await t.aclose()
        writer._jkbms_server.close()  # type: ignore[attr-defined]
        await writer._jkbms_server.wait_closed()  # type: ignore[attr-defined]


async def test_custom_baud_rate() -> None:
    reader, writer = await _build_in_memory_pair()
    try:
        opener = _make_fake_opener(reader, writer)
        t = UsbSerialTransport(device_path="/dev/x", baud_rate=9600)
        t._opener = opener
        await t.connect()
        assert opener.last_kwargs["baudrate"] == 9600  # type: ignore[attr-defined]
    finally:
        await t.aclose()
        writer._jkbms_server.close()  # type: ignore[attr-defined]
        await writer._jkbms_server.wait_closed()  # type: ignore[attr-defined]


async def test_write_then_read_round_trip() -> None:
    reader, writer = await _build_in_memory_pair()
    try:
        opener = _make_fake_opener(reader, writer)
        t = UsbSerialTransport(device_path="/dev/x")
        t._opener = opener
        await t.connect()
        await t.write(b"abc")
        out = await t.read_exactly(5, timeout_s=2.0)
        assert out == b"E:abc"
    finally:
        await t.aclose()
        writer._jkbms_server.close()  # type: ignore[attr-defined]
        await writer._jkbms_server.wait_closed()  # type: ignore[attr-defined]


async def test_idempotent_connect() -> None:
    reader, writer = await _build_in_memory_pair()
    try:
        opener = _make_fake_opener(reader, writer)
        t = UsbSerialTransport(device_path="/dev/x")
        t._opener = opener
        await t.connect()
        await t.connect()  # second call is a no-op
        assert t.is_connected
    finally:
        await t.aclose()
        writer._jkbms_server.close()  # type: ignore[attr-defined]
        await writer._jkbms_server.wait_closed()  # type: ignore[attr-defined]


async def test_aclose_idempotent() -> None:
    reader, writer = await _build_in_memory_pair()
    opener = _make_fake_opener(reader, writer)
    t = UsbSerialTransport(device_path="/dev/x")
    t._opener = opener
    await t.connect()
    await t.aclose()
    await t.aclose()
    assert not t.is_connected
    writer._jkbms_server.close()  # type: ignore[attr-defined]
    await writer._jkbms_server.wait_closed()  # type: ignore[attr-defined]


async def test_write_when_not_connected_raises() -> None:
    t = UsbSerialTransport(device_path="/dev/never")
    with pytest.raises(ConnectionError):
        await t.write(b"x")


async def test_read_when_not_connected_raises() -> None:
    t = UsbSerialTransport(device_path="/dev/never")
    with pytest.raises(ConnectionError):
        await t.read_exactly(1, timeout_s=0.5)


async def test_connect_failure_propagates() -> None:
    async def failing_opener(**_kwargs: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise OSError("device not found")

    t = UsbSerialTransport(device_path="/dev/missing")
    t._opener = failing_opener
    with pytest.raises(OSError, match="device not found"):
        await t.connect()


async def test_connect_with_backoff_uses_label(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    import logging

    async def _no_sleep(_d: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    caplog.set_level(logging.WARNING)

    async def failing_opener(**_kwargs: object) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise OSError("nope")

    t = UsbSerialTransport(device_path="/dev/x")
    t._opener = failing_opener
    with pytest.raises(OSError):
        await connect_with_backoff(t, max_attempts=2)
    assert "USB serial" in caplog.text


def test_default_opener_returns_callable_when_pkg_installed() -> None:
    """Sanity check the lazy import path. If pyserial-asyncio-fast isn't installed,
    this skips rather than fails — it's a runtime dep, not a hard requirement."""
    try:
        opener = _default_opener()
    except RuntimeError:
        pytest.skip("pyserial-asyncio-fast not installed in this environment")
    assert callable(opener)
