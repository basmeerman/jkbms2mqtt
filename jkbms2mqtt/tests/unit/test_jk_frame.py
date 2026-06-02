"""Tests for the JK reply-frame parser."""

from __future__ import annotations

from jkbms2mqtt.protocol.jk_frame import (
    MAGIC,
    FrameType,
    JkFrame,
    MalformedFrame,
    compute_checksum,
    parse_jk_frame,
)


class TestParseJkFrame:
    def test_valid_live_frame_round_trip(self, live_frame) -> None:
        raw = live_frame()
        result = parse_jk_frame(raw)
        assert isinstance(result, JkFrame)
        assert result.frame_type is FrameType.LIVE
        assert result.raw == raw
        assert result.unit_no == 0

    def test_valid_setup_frame(self, setup_frame) -> None:
        result = parse_jk_frame(setup_frame())
        assert isinstance(result, JkFrame)
        assert result.frame_type is FrameType.SETUP

    def test_valid_fixed_frame(self, fixed_frame) -> None:
        result = parse_jk_frame(fixed_frame())
        assert isinstance(result, JkFrame)
        assert result.frame_type is FrameType.FIXED

    def test_too_short_returns_malformed(self) -> None:
        result = parse_jk_frame(b"\x55\xaa")
        assert isinstance(result, MalformedFrame)
        assert "too short" in result.reason

    def test_bad_magic_returns_malformed(self) -> None:
        result = parse_jk_frame(b"\x00" * 300)
        assert isinstance(result, MalformedFrame)
        assert "bad magic" in result.reason

    def test_unknown_type_byte(self, live_frame) -> None:
        raw = bytearray(live_frame())
        raw[4] = 0xFF  # unknown type
        # Re-compute checksum so the type-byte branch (not checksum branch) is tested.
        raw[-1] = compute_checksum(bytes(raw[:-1]))
        result = parse_jk_frame(bytes(raw))
        assert isinstance(result, MalformedFrame)
        assert "unknown frame type" in result.reason

    def test_expected_type_enforced(self, live_frame) -> None:
        raw = live_frame()
        result = parse_jk_frame(raw, expected_type=FrameType.SETUP)
        assert isinstance(result, MalformedFrame)
        assert "expected SETUP" in result.reason
        assert "got LIVE" in result.reason

    def test_expected_type_accepted(self, live_frame) -> None:
        result = parse_jk_frame(live_frame(), expected_type=FrameType.LIVE)
        assert isinstance(result, JkFrame)

    def test_length_not_in_known_set(self, live_frame) -> None:
        # Take a valid frame and truncate it by one byte → length 299
        raw = live_frame()[:-1]
        # Replace what is now the last byte with a recomputed checksum so we hit the LENGTH
        # check, not the checksum check. The length check runs before the checksum check.
        result = parse_jk_frame(raw)
        assert isinstance(result, MalformedFrame)
        assert "frame length 299" in result.reason

    def test_checksum_mismatch(self, live_frame) -> None:
        raw = bytearray(live_frame())
        raw[-1] ^= 0xFF
        result = parse_jk_frame(bytes(raw))
        assert isinstance(result, MalformedFrame)
        assert "checksum mismatch" in result.reason

    def test_unit_no_decoded(self, live_frame) -> None:
        # The fixture's make_frame writes unit_no=0 at offset (len-5). Override by hand.
        raw = bytearray(live_frame())
        # Encode unit_no = 7 at offset 295 (len 300 - 5)
        raw[295:299] = (7).to_bytes(4, "little")
        raw[-1] = compute_checksum(bytes(raw[:-1]))
        result = parse_jk_frame(bytes(raw))
        assert isinstance(result, JkFrame)
        assert result.unit_no == 7

    def test_compute_checksum_xor_identity(self) -> None:
        assert compute_checksum(b"") == 0
        assert compute_checksum(b"\x01") == 0x01
        assert compute_checksum(b"\x01\x02\x03") == 0x00  # 1^2=3, 3^3=0

    def test_magic_constant_is_what_jk_uses(self) -> None:
        assert MAGIC == b"\x55\xaa\xeb\x90"
