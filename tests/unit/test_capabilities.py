"""Capability matrix tests — guards the policy that no impossible writes are advertised."""

from __future__ import annotations

import pytest

from jkbms2mqtt.protocol.capabilities import (
    Capabilities,
    Topology,
    Transport,
    is_valid_combo,
    lookup,
)


@pytest.mark.parametrize(
    ("transport", "topology", "expected_writes"),
    [
        (Transport.TCP_GATEWAY, Topology.MASTER_POLL, True),
        (Transport.TCP_GATEWAY, Topology.BROADCAST, False),
        (Transport.USB_SERIAL, Topology.MASTER_POLL, True),
        (Transport.USB_SERIAL, Topology.BROADCAST, False),
        (Transport.CAN_BUS, Topology.CAN, False),
    ],
)
def test_known_combinations(
    transport: Transport, topology: Topology, expected_writes: bool
) -> None:
    caps = lookup(transport, topology)
    assert isinstance(caps, Capabilities)
    assert caps.reads is True
    assert caps.writes is expected_writes
    if not expected_writes:
        assert caps.reason_if_no_writes is not None and caps.reason_if_no_writes


def test_unknown_combo_returns_none() -> None:
    # CAN bus with master_poll is nonsensical
    assert lookup(Transport.CAN_BUS, Topology.MASTER_POLL) is None
    assert not is_valid_combo(Transport.CAN_BUS, Topology.MASTER_POLL)


def test_valid_combo_predicate() -> None:
    assert is_valid_combo(Transport.TCP_GATEWAY, Topology.MASTER_POLL)
    assert not is_valid_combo(Transport.TCP_GATEWAY, Topology.CAN)


def test_no_write_capability_carries_user_visible_reason() -> None:
    # Every (transport, topology) with writes=False must explain why.
    from jkbms2mqtt.protocol import capabilities

    for (transport, topology), caps in capabilities._TABLE.items():
        if not caps.writes:
            assert caps.reason_if_no_writes, (
                f"{transport} / {topology} disables writes but has no reason"
            )
        else:
            assert caps.reason_if_no_writes is None
