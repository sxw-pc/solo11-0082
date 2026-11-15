import pytest

from pointlog.crc import append_crc
from pointlog.modbus import (
    FrameParseError,
    build_exception_response,
    build_request,
    build_response,
    corrupt_crc,
    exception_description,
    hex_to_bytes,
    parse_request,
    parse_response,
)


def test_request_round_trip():
    raw = build_request(1, 3, 2, 1)
    assert len(raw) == 8
    request = parse_request(raw)
    assert (request.slave_id, request.function, request.start, request.count) == (1, 3, 2, 1)
    assert request.crc_ok
    assert request.raw == raw
    assert request.register_span == (2, 1)


def test_request_crc_failure_is_reported_not_raised():
    request = parse_request(corrupt_crc(build_request(2, 4, 20, 2)))
    assert not request.crc_ok


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "empty_frame"),
        ("010", "odd_hex"),
        ("01 03 ZZ", "invalid_hex"),
        (123, "invalid_hex"),
    ],
)
def test_hex_decoding_errors(text, code):
    with pytest.raises(FrameParseError) as excinfo:
        hex_to_bytes(text)
    assert excinfo.value.code == code


def test_hex_allows_whitespace_and_uppercase():
    assert hex_to_bytes("01 03 00 00 00 01").hex() == "010300000001"


def test_request_structural_errors():
    with pytest.raises(FrameParseError) as short:
        parse_request(build_request(1, 3, 0, 1)[:-1])
    assert short.value.code == "truncated_frame"
    with pytest.raises(FrameParseError) as long:
        parse_request(build_request(1, 3, 0, 1) + b"\x00")
    assert long.value.code == "length_mismatch"
    with pytest.raises(FrameParseError) as function:
        parse_request(append_crc(bytes.fromhex("010500000001")))
    assert function.value.code == "unsupported_function"


def test_response_round_trip_and_crc_failure():
    response = parse_response(build_response(2, 4, bytes.fromhex("1234")))
    assert response.slave_id == 2
    assert response.function == 4
    assert response.byte_count == 2
    assert response.data == bytes.fromhex("1234")
    assert response.crc_ok
    assert not response.is_exception
    broken = parse_response(corrupt_crc(build_response(2, 4, b"\x00\x01")))
    assert not broken.crc_ok


def test_response_length_mismatch_variants():
    truncated = build_response(1, 3, b"\x00\x01\x00\x02")[:-2]
    with pytest.raises(FrameParseError) as excinfo:
        parse_response(truncated)
    assert excinfo.value.code == "truncated_frame"
    trailing = build_response(1, 3, b"\x00\x01") + b"\x99"
    with pytest.raises(FrameParseError) as excinfo:
        parse_response(trailing)
    assert excinfo.value.code == "length_mismatch"


def test_response_odd_byte_count():
    raw = append_crc(bytes((1, 3, 3, 1, 2, 3)))
    with pytest.raises(FrameParseError) as excinfo:
        parse_response(raw)
    assert excinfo.value.code == "odd_byte_count"


def test_exception_response_parsing():
    raw = build_exception_response(2, 3, 4)
    response = parse_response(raw)
    assert response.is_exception
    assert response.function == 0x83
    assert response.exception_code == 4
    assert response.data == b""
    assert response.crc_ok
    assert exception_description(4) == "device failure"
    assert exception_description(99).startswith("unknown")


def test_exception_response_must_be_five_bytes():
    with pytest.raises(FrameParseError) as excinfo:
        parse_response(build_exception_response(1, 4, 2) + b"\x00")
    assert excinfo.value.code == "length_mismatch"


def test_corrupt_crc_requires_two_bytes():
    with pytest.raises(ValueError):
        corrupt_crc(b"\x00")
