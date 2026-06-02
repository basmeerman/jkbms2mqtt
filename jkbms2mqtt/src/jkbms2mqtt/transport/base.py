"""Transport Protocol — the contract every transport implementation honours.

We model a transport as a bidirectional byte stream with explicit timeouts on
the read side. There is no separate "open" method; instead, transports are
constructed in a "not connected" state and `connect()` performs the actual
network / serial open. This lets the orchestrator instantiate transports up
front and `await connect()` later, retrying on failure.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Abstract bidirectional byte transport.

    Implementations: `TcpGatewayTransport` (v1), `UsbSerialTransport`,
    `RtuListenTransport`, `CanBusTransport` (v1.x).
    """

    async def connect(self) -> None:  # pragma: no cover - Protocol body
        """Open the underlying connection. May raise on failure."""
        ...

    async def read_exactly(self, n: int, *, timeout_s: float) -> bytes:  # pragma: no cover
        """Read exactly *n* bytes. Raise `asyncio.TimeoutError` if `timeout_s` elapses."""
        ...

    async def write(self, data: bytes) -> None:  # pragma: no cover
        """Write *data* to the transport. Returns when the bytes are queued."""
        ...

    async def aclose(self) -> None:  # pragma: no cover
        """Close the underlying connection. Idempotent."""
        ...

    @property
    def is_connected(self) -> bool:  # pragma: no cover
        """Whether the transport is currently connected."""
        ...
