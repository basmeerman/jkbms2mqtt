"""Reusable exponential-backoff connect helper.

Generic over any transport that exposes an async `connect()` raising `OSError`
or `TimeoutError` on failure. Used by both TCP gateway and USB serial.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 30.0


class _Connectable(Protocol):
    async def connect(self) -> None: ...  # pragma: no cover - Protocol body


async def connect_with_backoff(
    transport: _Connectable,
    *,
    max_attempts: int | None = None,
    label: str = "transport",
) -> None:
    """Connect with exponential backoff.

    `max_attempts=None` means retry forever (production). Tests pass a finite count.
    """
    attempt = 0
    backoff = INITIAL_BACKOFF_S
    while True:
        attempt += 1
        try:
            await transport.connect()
            return
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "%s connect attempt %d failed: %s — retrying in %.1fs",
                label,
                attempt,
                exc,
                backoff,
            )
            if max_attempts is not None and attempt >= max_attempts:
                raise
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_S)
