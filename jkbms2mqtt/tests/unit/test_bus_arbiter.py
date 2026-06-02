"""BusArbiter tests — guarantees no overlap between concurrent transactions on
the same bus, so a write never collides with an in-flight poll."""

from __future__ import annotations

import asyncio

import pytest

from jkbms2mqtt.bus_arbiter import BusArbiter


async def test_two_transactions_are_serialised() -> None:
    arbiter = BusArbiter(inter_frame_gap_ms=0)
    order: list[str] = []

    async def hold_for(name: str, ms: int) -> None:
        async with arbiter.transaction():
            order.append(f"{name}-enter")
            await asyncio.sleep(ms / 1000)
            order.append(f"{name}-leave")

    await asyncio.gather(hold_for("a", 50), hold_for("b", 50))
    # Whichever ran first must leave before the other enters.
    enter_idx = {o.split("-")[0]: i for i, o in enumerate(order) if "enter" in o}
    leave_idx = {o.split("-")[0]: i for i, o in enumerate(order) if "leave" in o}
    a, b = sorted(enter_idx.keys(), key=lambda k: enter_idx[k])
    assert leave_idx[a] < enter_idx[b]


async def test_inter_frame_gap_enforced() -> None:
    """A 100 ms inter-frame gap means the second acquirer must wait."""
    import time

    arbiter = BusArbiter(inter_frame_gap_ms=100)
    timestamps: list[float] = []

    async def quick() -> None:
        async with arbiter.transaction():
            timestamps.append(time.monotonic())

    await quick()
    await quick()
    assert timestamps[1] - timestamps[0] >= 0.09  # tolerate clock jitter


async def test_no_gap_means_no_wait() -> None:
    """Setting inter_frame_gap_ms=0 means consecutive transactions are back-to-back."""
    import time

    arbiter = BusArbiter(inter_frame_gap_ms=0)
    timestamps: list[float] = []

    async def quick() -> None:
        async with arbiter.transaction():
            timestamps.append(time.monotonic())

    await quick()
    await quick()
    assert timestamps[1] - timestamps[0] < 0.05


def test_negative_gap_rejected() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        BusArbiter(inter_frame_gap_ms=-1)


async def test_exception_inside_transaction_releases_lock() -> None:
    arbiter = BusArbiter(inter_frame_gap_ms=0)
    with pytest.raises(RuntimeError, match="boom"):
        async with arbiter.transaction():
            raise RuntimeError("boom")
    # Lock must be released — a follow-up transaction succeeds.
    async with arbiter.transaction():
        pass
