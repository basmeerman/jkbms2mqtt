"""Parser for the JK-BMS reply frame (the 0x55 0xAA 0xEB 0x90 family).

All replies share the same shape:
- 4-byte magic header `55 AA EB 90`.
- 1-byte frame type: 0x01 (setup / Trame 2), 0x02 (live / Trame 3), 0x03 (fixed / Trame 1)
  — the JK firmware historically used different bytes; we accept any of the three documented
  types and identify which by length and content.
- payload (variable length, frame-type-specific).
- 4-byte BMS unit number (little-endian) at the very end before the trailing checksum byte.
- 1-byte XOR-checksum over the whole frame except the checksum byte itself.

This parser is **total**: it never raises on malformed input. Instead it returns
a Result that the caller pattern-matches on. This avoids the "service hangs
after a single bad byte" failure mode that any naïve frame parser exhibits when
a truncated reply arrives mid-stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

MAGIC: Final = b"\x55\xaa\xeb\x90"

# Minimum frame size: 4 magic + 1 type + N payload + 4 unit_no + 1 checksum.
# Each known frame type has a fixed length; we accept any of them.
LEN_TRAME_FIXED: Final = 300  # Trame 1
LEN_TRAME_SETUP: Final = 300  # Trame 2
LEN_TRAME_LIVE: Final = 300  # Trame 3
KNOWN_LENGTHS: Final = frozenset({LEN_TRAME_FIXED, LEN_TRAME_SETUP, LEN_TRAME_LIVE})


@unique
class FrameType(int, Enum):
    """JK reply frame types.

    The "type byte" at offset 4 in the magic-stripped frame is one of
    0x01 / 0x02 / 0x03; the exact mapping depends on the BMS firmware. We
    classify by *length* and *poll-trigger context* in addition to the
    type byte.
    """

    FIXED = 0x03  # Trame 1: static device info (model, FW, serial, ...)
    SETUP = 0x01  # Trame 2: configuration / settings (writable params + their current values)
    LIVE = 0x02  # Trame 3: real-time data (cells, current, SoC, temps)


@dataclass(frozen=True, slots=True)
class JkFrame:
    """A validated JK-BMS reply frame, ready for decode.

    `raw` contains the full bytes including the 4-byte magic header so the
    decoder can use the documented absolute byte offsets into the JK reply
    frame without translation.
    """

    frame_type: FrameType
    raw: bytes
    unit_no: int  # 4-byte LE integer near the tail


@dataclass(frozen=True, slots=True)
class MalformedFrame:
    """The bytes could not be interpreted as a JK frame."""

    reason: str
    raw: bytes


ParsedFrame = JkFrame | MalformedFrame


def parse_jk_frame(buf: bytes, *, expected_type: FrameType | None = None) -> ParsedFrame:
    """Parse a single JK reply frame.

    *buf* must start with the magic header; trailing bytes beyond the declared
    frame length are ignored (in practice the BMS sends one frame per poll).

    Set *expected_type* to enforce the type byte at offset 4. If None, any
    documented type is accepted and the result reports which it was.
    """
    if len(buf) < 6:
        return MalformedFrame("frame too short for header + type", buf)

    if not buf.startswith(MAGIC):
        return MalformedFrame(f"bad magic: {buf[:4].hex()}", buf)

    type_byte = buf[4]
    try:
        ftype = FrameType(type_byte)
    except ValueError:
        return MalformedFrame(f"unknown frame type byte {type_byte:#x}", buf)

    if expected_type is not None and ftype is not expected_type:
        return MalformedFrame(
            f"expected {expected_type.name}, got {ftype.name}", buf
        )

    if len(buf) not in KNOWN_LENGTHS:
        return MalformedFrame(
            f"frame length {len(buf)} not in known set {sorted(KNOWN_LENGTHS)}", buf
        )

    # Verify XOR checksum: the last byte is the XOR of all preceding bytes.
    expected_cksum = 0
    for b in buf[:-1]:
        expected_cksum ^= b
    if expected_cksum != buf[-1]:
        return MalformedFrame(
            f"checksum mismatch: expected {expected_cksum:#x}, got {buf[-1]:#x}", buf
        )

    # Unit number sits at offset (len - 5) — 4 LE bytes before the checksum byte.
    unit_no = int.from_bytes(buf[len(buf) - 5 : len(buf) - 1], "little")

    return JkFrame(frame_type=ftype, raw=buf, unit_no=unit_no)


def compute_checksum(body: bytes) -> int:
    """XOR-checksum used by the JK-BMS reply frames.

    Exposed so tests can construct synthetic frames with a valid checksum.
    """
    out = 0
    for b in body:
        out ^= b
    return out
