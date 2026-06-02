"""BusArbiter — serialises transactions on the RS485 bus.

A "transaction" is one Modbus request followed by exactly one matched reply
(either a JK frame or a Modbus write-ack). The arbiter ensures:

1. **No interleaving**: write_executor's writes cannot overlap with poll_loop's
   reads, because both must acquire `transaction()` before touching the
   transport. Without this, writes that collide with an in-flight poll on the
   bus get silently dropped by the BMS.
2. **Inter-frame gap**: a configurable minimum delay between releasing the lock
   and the next acquirer. The default 50 ms reflects observed RS485 line
   turnaround behaviour; can be tuned via config.

Usage:
    async with arbiter.transaction():
        await transport.write(req)
        reply = await transport.read_exactly(n, timeout_s=...)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class BusArbiter:
    def __init__(self, inter_frame_gap_ms: int = 50) -> None:
        if inter_frame_gap_ms < 0:
            raise ValueError("inter_frame_gap_ms must be >= 0")
        self._lock = asyncio.Lock()
        self._inter_frame_gap_s = inter_frame_gap_ms / 1000.0
        self._last_release_ts: float = 0.0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        await self._lock.acquire()
        try:
            # Honour inter-frame gap if the previous transaction ended recently.
            wait_for = self._last_release_ts + self._inter_frame_gap_s - time.monotonic()
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            yield
        finally:
            self._last_release_ts = time.monotonic()
            self._lock.release()
