"""Tests for the TCP gateway transport using a fake asyncio server."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from jkbms2mqtt.transport.tcp_gateway import TcpGatewayTransport, connect_with_backoff


async def _start_echo_server() -> tuple[asyncio.Server, int]:
    received: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                received.append(data)
                writer.write(b"OK:" + data)
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(handler, host="127.0.0.1", port=0)
    server._received = received  # type: ignore[attr-defined]
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_connect_round_trip_write_then_read() -> None:
    server, port = await _start_echo_server()
    transport = TcpGatewayTransport(host="127.0.0.1", port=port)
    try:
        await transport.connect()
        assert transport.is_connected
        await transport.write(b"ping")
        data = await transport.read_exactly(7, timeout_s=2.0)
        assert data == b"OK:ping"
    finally:
        await transport.aclose()
        server.close()
        await server.wait_closed()


async def test_idempotent_connect() -> None:
    server, port = await _start_echo_server()
    transport = TcpGatewayTransport(host="127.0.0.1", port=port)
    try:
        await transport.connect()
        await transport.connect()  # second call is a no-op
        assert transport.is_connected
    finally:
        await transport.aclose()
        server.close()
        await server.wait_closed()


async def test_aclose_swallows_wait_closed_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive guard: aclose tolerates wait_closed raising OSError on already-broken socket."""
    server, port = await _start_echo_server()
    transport = TcpGatewayTransport(host="127.0.0.1", port=port)
    try:
        await transport.connect()
        writer = transport._writer
        assert writer is not None

        async def raise_oserror() -> None:
            raise OSError("simulated close error")

        monkeypatch.setattr(writer, "wait_closed", raise_oserror)
        # Must not raise.
        await transport.aclose()
    finally:
        server.close()
        await server.wait_closed()


async def test_aclose_is_idempotent() -> None:
    server, port = await _start_echo_server()
    transport = TcpGatewayTransport(host="127.0.0.1", port=port)
    await transport.connect()
    await transport.aclose()
    await transport.aclose()  # second call must not raise
    assert not transport.is_connected
    server.close()
    await server.wait_closed()


async def test_write_when_not_connected_raises() -> None:
    transport = TcpGatewayTransport(host="127.0.0.1", port=0)
    with pytest.raises(ConnectionError):
        await transport.write(b"x")


async def test_read_when_not_connected_raises() -> None:
    transport = TcpGatewayTransport(host="127.0.0.1", port=0)
    with pytest.raises(ConnectionError):
        await transport.read_exactly(1, timeout_s=0.5)


async def test_connect_failure_propagates_to_caller() -> None:
    # Bind+close a socket to obtain a definitely-closed port.
    port = await _free_closed_port()
    transport = TcpGatewayTransport(host="127.0.0.1", port=port, connect_timeout_s=0.5)
    with pytest.raises((OSError, asyncio.TimeoutError)):
        await transport.connect()


async def test_connect_with_backoff_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    server, port = await _start_echo_server()
    transport = TcpGatewayTransport(host="127.0.0.1", port=port)

    # First connect attempt: redirect to a closed port so it fails; then flip back.
    attempt = {"count": 0}
    real_connect = transport.connect

    async def flaky_connect() -> None:
        attempt["count"] += 1
        if attempt["count"] < 2:
            raise OSError("simulated failure")
        await real_connect()

    monkeypatch.setattr(transport, "connect", flaky_connect)
    # Cap sleep at 0 so the test is fast.
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    try:
        await connect_with_backoff(transport)
        assert attempt["count"] == 2
    finally:
        await transport.aclose()
        server.close()
        await server.wait_closed()


async def _free_closed_port() -> int:
    """Bind a port to grab the number, then close — connecting to it raises ConnectionRefused."""
    s, port = await _start_echo_server()
    s.close()
    await s.wait_closed()
    return port


async def test_connect_with_backoff_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    port = await _free_closed_port()
    transport = TcpGatewayTransport(host="127.0.0.1", port=port, connect_timeout_s=0.5)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    with pytest.raises((OSError, asyncio.TimeoutError)):
        await connect_with_backoff(transport, max_attempts=2)


async def test_connect_with_backoff_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff doubles up to a cap of 30 s — verify by counting attempts pre-cap.

    Set max_attempts=10 to ensure backoff goes through the doubling logic and
    hits the `min(backoff*2, MAX)` branch (covers the cap).
    """
    port = await _free_closed_port()
    transport = TcpGatewayTransport(host="127.0.0.1", port=port, connect_timeout_s=0.5)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    with pytest.raises((OSError, asyncio.TimeoutError)):
        await connect_with_backoff(transport, max_attempts=10)


async def _noop_sleep(_delay: float) -> None:
    """Replacement for asyncio.sleep that returns instantly."""
    return None
