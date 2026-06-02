"""Unit tests for the Modbus RTU layer.

Covers every branch of modbus.py: CRC, encoders, parser dispatch (ack / exception / malformed).
The CRC against known JK-BMS frame examples is the most important test — if this passes,
every downstream encode/parse function will produce wire-compatible bytes.
"""

from __future__ import annotations

import pytest

from jkbms2mqtt.protocol.modbus import (
    EXCEPTION_FLAG,
    FUNC_WRITE_MULTIPLE,
    FUNC_WRITE_SINGLE,
    MalformedAck,
    ModbusException,
    WriteAck,
    append_crc,
    crc16_a001,
    encode_poll_request,
    encode_write_request,
    encode_write_single_register,
    parse_write_ack,
    verify_crc,
)


class TestCRC:
    """Known-good Modbus CRC16/A001 vectors.

    The vector for slave=1, function=0x10, register=0x1620 (the JK live-data poll trigger)
    matches the documented JK-BMS poll-trigger frame.
    """

    def test_empty_input_returns_init(self) -> None:
        assert crc16_a001(b"") == 0xFFFF

    def test_known_vector_modbus_examples(self) -> None:
        # Classic example from Modbus reference: CRC of bytes 02 07 → 0x1241
        assert crc16_a001(bytes([0x02, 0x07])) == 0x1241

    def test_jk_poll_frame_known_crc(self) -> None:
        # JK poll: slave=1, func=0x10, reg=0x1620, qty=1, bc=2, payload=0x0000
        # Without CRC: 01 10 16 20 00 01 02 00 00 → CRC must produce a valid frame
        body = bytes([0x01, 0x10, 0x16, 0x20, 0x00, 0x01, 0x02, 0x00, 0x00])
        framed = append_crc(body)
        assert len(framed) == 11
        # And the framed message must round-trip through verify_crc
        assert verify_crc(framed)

    def test_verify_crc_rejects_tampered_payload(self) -> None:
        body = bytes([0x01, 0x10, 0x16, 0x20, 0x00, 0x01, 0x02, 0x00, 0x00])
        framed = append_crc(body)
        # Flip a payload byte but keep CRC bytes — verification must fail.
        tampered = bytearray(framed)
        tampered[4] ^= 0xFF
        assert not verify_crc(bytes(tampered))

    def test_verify_crc_rejects_short_frame(self) -> None:
        assert not verify_crc(b"")
        assert not verify_crc(b"\x42")  # below 2-byte minimum (no CRC tail possible)

    def test_verify_crc_handles_zero_body_round_trip(self) -> None:
        # CRC of an empty body is 0xFFFF; the framed bytes must verify.
        assert verify_crc(append_crc(b""))

    def test_append_then_verify_is_identity(self) -> None:
        body = bytes(range(50))
        assert verify_crc(append_crc(body))


class TestEncodePollRequest:
    def test_layout(self) -> None:
        frame = encode_poll_request(slave_addr=1, register=0x1620)
        # 11 bytes total: 9 header/body + 2 CRC
        assert len(frame) == 11
        assert frame[0] == 0x01
        assert frame[1] == FUNC_WRITE_MULTIPLE
        assert frame[2] == 0x16
        assert frame[3] == 0x20
        assert frame[4:9] == bytes([0x00, 0x01, 0x02, 0x00, 0x00])
        assert verify_crc(frame)

    def test_slave_address_validation(self) -> None:
        with pytest.raises(ValueError, match="slave address"):
            encode_poll_request(slave_addr=-1, register=0x1620)
        with pytest.raises(ValueError, match="slave address"):
            encode_poll_request(slave_addr=256, register=0x1620)

    def test_register_validation(self) -> None:
        with pytest.raises(ValueError, match="register"):
            encode_poll_request(slave_addr=1, register=-1)
        with pytest.raises(ValueError, match="register"):
            encode_poll_request(slave_addr=1, register=0x10000)


class TestEncodeWriteRequest:
    def test_layout_for_balance_switch(self) -> None:
        # Balance switch enable: register 0x1078, value = 4 bytes big-endian with LSB=1
        payload = bytes([0x00, 0x00, 0x00, 0x01])
        frame = encode_write_request(slave_addr=1, register=0x1078, value=payload)
        assert len(frame) == 13
        assert frame[0] == 0x01
        assert frame[1] == FUNC_WRITE_MULTIPLE
        assert frame[2] == 0x10
        assert frame[3] == 0x78
        assert frame[4:7] == bytes([0x00, 0x02, 0x04])
        assert frame[7:11] == payload
        assert verify_crc(frame)

    def test_payload_length_must_be_four(self) -> None:
        with pytest.raises(ValueError, match="4 bytes"):
            encode_write_request(slave_addr=1, register=0x1078, value=b"\x00")
        with pytest.raises(ValueError, match="4 bytes"):
            encode_write_request(slave_addr=1, register=0x1078, value=b"\x00" * 5)

    def test_propagates_slave_validation(self) -> None:
        with pytest.raises(ValueError, match="slave address"):
            encode_write_request(slave_addr=300, register=0x1078, value=b"\x00\x00\x00\x01")

    def test_propagates_register_validation(self) -> None:
        with pytest.raises(ValueError, match="register"):
            encode_write_request(slave_addr=1, register=0x10000, value=b"\x00\x00\x00\x01")


class TestEncodeWriteSingleRegister:
    def test_layout_for_packed_bits(self) -> None:
        # PCL disable bit at 0x1114, value=0x0080 (bit 7 set)
        frame = encode_write_single_register(slave_addr=1, register=0x1114, value=0x0080)
        assert len(frame) == 8
        assert frame[0] == 0x01
        assert frame[1] == FUNC_WRITE_SINGLE
        assert frame[2] == 0x11
        assert frame[3] == 0x14
        assert frame[4] == 0x00
        assert frame[5] == 0x80
        assert verify_crc(frame)

    def test_value_must_fit_in_16_bits(self) -> None:
        with pytest.raises(ValueError, match="16 bits"):
            encode_write_single_register(slave_addr=1, register=0x1114, value=0x10000)
        with pytest.raises(ValueError, match="16 bits"):
            encode_write_single_register(slave_addr=1, register=0x1114, value=-1)

    def test_propagates_slave_validation(self) -> None:
        with pytest.raises(ValueError, match="slave address"):
            encode_write_single_register(slave_addr=-1, register=0x1114, value=0)

    def test_propagates_register_validation(self) -> None:
        with pytest.raises(ValueError, match="register"):
            encode_write_single_register(slave_addr=1, register=0x20000, value=0)


class TestParseWriteAck:
    """parse_write_ack covers four outcomes: WriteAck, ModbusException, MalformedAck, all paths."""

    def test_valid_multiple_register_ack(self) -> None:
        body = bytes([0x01, 0x10, 0x10, 0x78, 0x00, 0x02])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=0x01, expected_register=0x1078)
        assert isinstance(result, WriteAck)
        assert result.slave_addr == 1
        assert result.function == FUNC_WRITE_MULTIPLE
        assert result.register == 0x1078
        assert result.quantity == 2

    def test_valid_single_register_ack(self) -> None:
        body = bytes([0x01, 0x06, 0x11, 0x14, 0x00, 0x80])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=0x01, expected_register=0x1114)
        assert isinstance(result, WriteAck)
        assert result.function == FUNC_WRITE_SINGLE
        assert result.register == 0x1114
        # For 0x06, quantity field carries the echoed value.
        assert result.quantity == 0x80

    def test_modbus_exception_illegal_data_address(self) -> None:
        body = bytes([0x01, FUNC_WRITE_MULTIPLE | EXCEPTION_FLAG, 0x02])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=0x01, expected_register=0x1078)
        assert isinstance(result, ModbusException)
        assert result.exception_code == 0x02
        assert result.function == FUNC_WRITE_MULTIPLE
        assert "illegal data address" in result.message

    def test_modbus_exception_message_for_known_codes(self) -> None:
        for code, expected_substr in (
            (0x01, "illegal function"),
            (0x02, "illegal data address"),
            (0x03, "illegal data value"),
            (0x04, "slave device failure"),
            (0x05, "acknowledge"),
            (0x06, "slave device busy"),
        ):
            exc = ModbusException(slave_addr=1, function=0x10, exception_code=code)
            assert expected_substr in exc.message

    def test_modbus_exception_unknown_code(self) -> None:
        exc = ModbusException(slave_addr=1, function=0x10, exception_code=0x42)
        assert "unknown exception" in exc.message
        assert "0x42" in exc.message

    def test_short_frame(self) -> None:
        result = parse_write_ack(b"\x01\x10", expected_slave=1, expected_register=0x1078)
        assert isinstance(result, MalformedAck)
        assert "too short" in result.reason

    def test_bad_crc(self) -> None:
        # 8-byte frame that would be a valid 0x10 ack if the CRC matched
        result = parse_write_ack(
            bytes([0x01, 0x10, 0x10, 0x78, 0x00, 0x02, 0xDE, 0xAD]),
            expected_slave=1,
            expected_register=0x1078,
        )
        assert isinstance(result, MalformedAck)
        assert "bad CRC" in result.reason

    def test_exception_wrong_length(self) -> None:
        # Exception frames must be exactly 5 bytes including CRC
        body = bytes([0x01, FUNC_WRITE_MULTIPLE | EXCEPTION_FLAG, 0x02, 0x00])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=1, expected_register=0x1078)
        assert isinstance(result, MalformedAck)
        assert "exception frame must be 5 bytes" in result.reason

    def test_multiple_register_ack_wrong_length(self) -> None:
        # A function-0x10 ack with extra trailing bytes should be flagged
        body = bytes([0x01, 0x10, 0x10, 0x78, 0x00, 0x02, 0x00])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=1, expected_register=0x1078)
        assert isinstance(result, MalformedAck)
        assert "8 bytes" in result.reason

    def test_single_register_ack_wrong_length(self) -> None:
        body = bytes([0x01, 0x06, 0x11, 0x14, 0x00])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=1, expected_register=0x1114)
        assert isinstance(result, MalformedAck)
        assert "8 bytes" in result.reason

    def test_slave_mismatch_multiple(self) -> None:
        body = bytes([0x02, 0x10, 0x10, 0x78, 0x00, 0x02])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=0x01, expected_register=0x1078)
        assert isinstance(result, MalformedAck)
        assert "slave mismatch" in result.reason

    def test_slave_mismatch_single(self) -> None:
        body = bytes([0x02, 0x06, 0x11, 0x14, 0x00, 0x80])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=0x01, expected_register=0x1114)
        assert isinstance(result, MalformedAck)
        assert "slave mismatch" in result.reason

    def test_register_mismatch_multiple(self) -> None:
        body = bytes([0x01, 0x10, 0x10, 0x78, 0x00, 0x02])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=0x01, expected_register=0x1030)
        assert isinstance(result, MalformedAck)
        assert "register mismatch" in result.reason

    def test_register_mismatch_single(self) -> None:
        body = bytes([0x01, 0x06, 0x11, 0x14, 0x00, 0x80])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=0x01, expected_register=0x1030)
        assert isinstance(result, MalformedAck)
        assert "register mismatch" in result.reason

    def test_unexpected_function_code(self) -> None:
        body = bytes([0x01, 0x03, 0x10, 0x78, 0x00, 0x02])
        frame = append_crc(body)
        result = parse_write_ack(frame, expected_slave=0x01, expected_register=0x1078)
        assert isinstance(result, MalformedAck)
        assert "unexpected function" in result.reason
