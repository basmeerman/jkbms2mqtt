"""FrameScanner tests."""

from __future__ import annotations

from jkbms2mqtt.protocol.scanner import FrameScanner


def test_complete_frame_in_one_chunk(live_frame) -> None:
    scanner = FrameScanner()
    raw = live_frame()
    frames = list(scanner.feed(raw))
    assert len(frames) == 1
    assert frames[0] == raw


def test_two_frames_in_one_chunk(live_frame, setup_frame) -> None:
    scanner = FrameScanner()
    raw = live_frame() + setup_frame()
    frames = list(scanner.feed(raw))
    assert len(frames) == 2


def test_frame_split_across_chunks(live_frame) -> None:
    scanner = FrameScanner()
    raw = live_frame()
    frames = list(scanner.feed(raw[:100]))
    assert frames == []
    frames = list(scanner.feed(raw[100:]))
    assert frames == [raw]


def test_leading_garbage_dropped(live_frame) -> None:
    scanner = FrameScanner()
    raw = live_frame()
    # Modbus poll-request bytes that precede a broadcast reply.
    garbage = b"\x01\x10\x16\x20\x00\x01\x02\x00\x00\xff\xff"
    frames = list(scanner.feed(garbage + raw))
    assert frames == [raw]


def test_no_magic_in_buffer_keeps_tail() -> None:
    scanner = FrameScanner()
    # Feed a long run of non-magic bytes; the scanner should hold only the
    # last (len(MAGIC) - 1) = 3 bytes so a magic spanning chunk boundary still works.
    list(scanner.feed(b"\x00" * 100))
    assert len(scanner._buf) == 3


def test_magic_split_across_chunk_boundary(live_frame) -> None:
    scanner = FrameScanner()
    raw = live_frame()
    # First chunk carries 3 of the 4 magic bytes
    list(scanner.feed(b"\x00" * 50 + raw[:3]))
    # Second chunk completes magic + full frame
    frames = list(scanner.feed(raw[3:]))
    assert frames == [raw]


def test_garbage_between_frames(live_frame, setup_frame) -> None:
    scanner = FrameScanner()
    out = list(scanner.feed(live_frame() + b"\xaa\xbb" + setup_frame()))
    assert len(out) == 2


def test_empty_feed_is_noop() -> None:
    scanner = FrameScanner()
    assert list(scanner.feed(b"")) == []
