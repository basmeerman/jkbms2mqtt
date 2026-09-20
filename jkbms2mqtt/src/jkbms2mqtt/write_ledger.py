"""Records when each parameter was last written, so stale reads never publish.

Issue #34. A settings poll captures the register block at one instant and
publishes it a moment later — the packed-bit read happens in between. A write
landing in that window has its new value echoed immediately by the write
executor, and is then overwritten by the poll's older capture. Home Assistant
shows the new value, then reverts to the old one, which reads as a failed write.

The guard is deliberately based on *ordering*, not on comparing values:

- Comparing against the last published value does not help, because a stale
  capture is a genuinely different value and passes any change filter.
- Comparing against the previous poll's value only works by luck, and breaks
  on rapid successive writes (holding an arrow key), which is exactly the
  case reported.

So the poll asks a simple question instead: *has this parameter been written
successfully since I captured it?* If yes, the capture is known-superseded and
is dropped. Nothing is suppressed on a timer, so a genuinely failed write is
still visible on the next cycle.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class WriteLedger:
    """Last successful write time per ``(bms_name, object_id)``.

    Shared between the write executor (which records) and every ``BmsRunner``
    (which consults). ``clock`` is injectable so tests need no real time.
    """

    clock: Callable[[], float] = time.monotonic
    _written_at: dict[tuple[str, str], float] = field(default_factory=dict)

    def mark(self, bms_name: str, object_id: str) -> None:
        """Record that *object_id* on *bms_name* was just written successfully."""
        self._written_at[(bms_name, object_id)] = self.clock()

    def written_since(self, bms_name: str, object_id: str, captured_at: float) -> bool:
        """True if a successful write landed after *captured_at*.

        Ties count as superseded: a capture and a write sharing a timestamp are
        indistinguishable in ordering, and echoing the write is the safer of
        the two — the next poll republishes the truth either way.
        """
        written = self._written_at.get((bms_name, object_id))
        return written is not None and written >= captured_at
