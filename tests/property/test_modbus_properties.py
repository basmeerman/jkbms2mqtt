"""Property-based tests for the Modbus layer.

These complement the example-based tests in tests/unit/test_modbus.py by exhaustively
exploring the input space.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from jkbms2mqtt.protocol.modbus import (
    WriteAck,
    append_crc,
    crc16_a001,
    encode_poll_request,
    encode_write_request,
    encode_write_single_register,
    parse_write_ack,
    verify_crc,
)


@given(st.binary(max_size=256))
def test_append_crc_then_verify_always_succeeds(data: bytes) -> None:
    assert verify_crc(append_crc(data))


@given(st.binary(min_size=3, max_size=256))
def test_flipping_any_payload_bit_breaks_crc(data: bytes) -> None:
    framed = append_crc(data)
    # Flip the high bit of byte 0 of the body — CRC must no longer verify.
    mutated = bytearray(framed)
    mutated[0] ^= 0x80
    assert not verify_crc(bytes(mutated))


@given(st.binary(max_size=256))
def test_crc_fits_in_16_bits(data: bytes) -> None:
    crc = crc16_a001(data)
    assert 0 <= crc <= 0xFFFF


@given(
    slave=st.integers(min_value=0, max_value=0xFF),
    register=st.integers(min_value=0, max_value=0xFFFF),
)
def test_poll_frame_shape_is_invariant(slave: int, register: int) -> None:
    frame = encode_poll_request(slave_addr=slave, register=register)
    assert len(frame) == 11
    assert frame[0] == slave
    assert frame[2] == (register >> 8) & 0xFF
    assert frame[3] == register & 0xFF
    assert verify_crc(frame)


@given(
    slave=st.integers(min_value=0, max_value=0xFF),
    register=st.integers(min_value=0, max_value=0xFFFF),
    payload=st.binary(min_size=4, max_size=4),
)
def test_write_frame_shape_is_invariant(slave: int, register: int, payload: bytes) -> None:
    frame = encode_write_request(slave_addr=slave, register=register, value=payload)
    assert len(frame) == 13
    assert frame[7:11] == payload
    assert verify_crc(frame)


@given(
    slave=st.integers(min_value=0, max_value=0xFF),
    register=st.integers(min_value=0, max_value=0xFFFF),
    value=st.integers(min_value=0, max_value=0xFFFF),
)
def test_single_register_frame_shape_is_invariant(slave: int, register: int, value: int) -> None:
    frame = encode_write_single_register(slave_addr=slave, register=register, value=value)
    assert len(frame) == 8
    assert verify_crc(frame)


@given(
    slave=st.integers(min_value=0, max_value=0xFF),
    register=st.integers(min_value=0, max_value=0xFFFF),
)
def test_round_trip_write_ack_parses(slave: int, register: int) -> None:
    # Build a valid 0x10 ack frame and confirm parse_write_ack accepts it.
    body = bytes(
        [
            slave,
            0x10,
            (register >> 8) & 0xFF,
            register & 0xFF,
            0x00,
            0x02,
        ]
    )
    framed = append_crc(body)
    result = parse_write_ack(framed, expected_slave=slave, expected_register=register)
    assert isinstance(result, WriteAck)
    assert result.slave_addr == slave
    assert result.register == register
