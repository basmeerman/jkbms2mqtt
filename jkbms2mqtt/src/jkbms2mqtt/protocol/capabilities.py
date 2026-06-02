"""Read / write capability matrix across transports and topologies.

The bridge consults this matrix to decide:
- Whether to attempt any writes at all (write-enable plus mode support).
- Whether to publish HA Discovery payloads for writable entities (`number`,
  `switch`, `select`) — we never advertise an entity we can't honour.

Hard-refuse policy: posting to a `/set` topic in a mode that does not support
writes results in a structured error message on `<bms_name>/error` and a
WARNING log; the bytes never touch the bus.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique


@unique
class Transport(str, Enum):
    TCP_GATEWAY = "tcp_gateway"
    USB_SERIAL = "usb_serial"
    RTU_LISTEN = "rtu_listen"
    CAN_BUS = "can_bus"


@unique
class Topology(str, Enum):
    MASTER_POLL = "master_poll"
    BROADCAST = "broadcast"
    CAN = "can"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Whether reads and writes are possible for a `(transport, topology)` pair."""

    reads: bool
    writes: bool
    reason_if_no_writes: str | None


_TABLE: dict[tuple[Transport, Topology], Capabilities] = {
    (Transport.TCP_GATEWAY, Topology.MASTER_POLL): Capabilities(
        reads=True, writes=True, reason_if_no_writes=None
    ),
    (Transport.TCP_GATEWAY, Topology.BROADCAST): Capabilities(
        reads=True,
        writes=False,
        reason_if_no_writes=(
            "broadcast / listen topology: BMS is the bus master — injecting writes "
            "would collide with its broadcasts"
        ),
    ),
    (Transport.USB_SERIAL, Topology.MASTER_POLL): Capabilities(
        reads=True, writes=True, reason_if_no_writes=None
    ),
    (Transport.USB_SERIAL, Topology.BROADCAST): Capabilities(
        reads=True,
        writes=False,
        reason_if_no_writes=(
            "broadcast / listen topology: BMS is the bus master — injecting writes "
            "would collide with its broadcasts"
        ),
    ),
    (Transport.CAN_BUS, Topology.CAN): Capabilities(
        reads=True,
        writes=False,
        reason_if_no_writes="CAN protocol is broadcast-only telemetry on this BMS",
    ),
}


def lookup(transport: Transport, topology: Topology) -> Capabilities | None:
    """Return the capability entry for the pair, or None if the combo is invalid."""
    return _TABLE.get((transport, topology))


def is_valid_combo(transport: Transport, topology: Topology) -> bool:
    """True if `(transport, topology)` is a configured, supported pair."""
    return (transport, topology) in _TABLE
