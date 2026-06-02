"""Stream-oriented frame scanner.

In broadcast / listen mode the JK-BMSes blast their JK reply frames onto the
bus whenever the bus master polls them; we just listen. The transport surfaces
bytes as an unframed stream, so we need to scan for the 0x55 0xAA 0xEB 0x90
magic header and pull off complete 300-byte frames.

The scanner is a pure-functional state machine with no I/O, no async — easy
to unit-test exhaustively.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from jkbms2mqtt.protocol.jk_frame import KNOWN_LENGTHS, MAGIC


@dataclass
class FrameScanner:
    """Accumulate bytes, yield complete JK reply frames.

    Usage:
        scanner = FrameScanner()
        for raw_frame in scanner.feed(rx_chunk):
            ...  # raw_frame is exactly one 300-byte JK frame

    The scanner tolerates leading garbage (poll request bytes, partial frames
    from a mid-stream join) by scanning for the magic header. Bytes that don't
    fit any frame are discarded.
    """

    _buf: bytearray = field(default_factory=bytearray)
    _frame_length: int = field(default=300, init=False)
    # The maximum amount of leading garbage we'll buffer while hunting for a
    # magic header before dropping. Keeps memory bounded if a transport is
    # spewing non-JK noise.
    max_garbage_bytes: int = 4096

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        """Append bytes; yield every complete frame found."""
        if chunk:
            self._buf.extend(chunk)
        while True:
            # Find the next magic header.
            idx = self._buf.find(MAGIC)
            if idx == -1:
                # No magic anywhere in the buffer. Keep the tail (up to len(MAGIC)-1
                # bytes) because the magic could span the next chunk boundary.
                if len(self._buf) > len(MAGIC) - 1:
                    keep = max(0, len(self._buf) - (len(MAGIC) - 1))
                    del self._buf[:keep]
                return
            # Drop any bytes before the magic (garbage).
            if idx > 0:
                del self._buf[:idx]
            if len(self._buf) < self._frame_length:
                # Magic found but the rest of the frame hasn't arrived yet.
                return
            frame = bytes(self._buf[: self._frame_length])
            if len(frame) in KNOWN_LENGTHS:
                yield frame
                del self._buf[: self._frame_length]
            else:  # pragma: no cover - only one length is in KNOWN_LENGTHS today
                # Safety net for future protocol variants: skip past this magic.
                del self._buf[:1]
            # If we somehow accumulated too much trailing garbage without a frame,
            # trim. (We only get here after a yield, so trimming is conservative.)
            if len(self._buf) > self.max_garbage_bytes:  # pragma: no cover - defensive
                del self._buf[: -len(MAGIC) + 1]
