import pytest

from pointlog.codec import (
    CodecError,
    apply_engineering,
    classify_range,
    decode_value,
    encode_raw,
    enum_label,
)


@pytest.mark.parametrize(
    ("data", "data_type", "order", "expected"),
    [
        ("1234", "uint16", "AB", 0x1234),
        ("3412", "uint16", "BA", 0x1234),
        ("fffe", "int16", "AB", -2),
        ("feff", "int16", "BA", -2),
        ("12345678", "uint32", "ABCD", 0x12345678),
        ("56781234", "uint32", "CDAB", 0x12345678),
        ("34127856", "uint32", "BADC", 0x12345678),
        ("78563412", "uint32", "DCBA", 0x12345678),
        ("fffffffe", "int32", "ABCD", -2),
        ("00000000", "uint32", "DCBA", 0),
    ],
)
def test_known_byte_orders(data, data_type, order, expected):
    assert decode_value(bytes.fromhex(data), data_type, order) == expected


@pytest.mark.parametrize(
    ("data", "order", "expected"),
    [
        ("3fc00000", "ABCD", 1.5),
        ("00003fc0", "CDAB", 1.5),
        ("c03f0000", "BADC", 1.5),
        ("0000c03f", "DCBA", 1.5),
    ],
)
def test_float_byte_orders(data, order, expected):
    assert decode_value(bytes.fromhex(data), "float32", order) == expected


@pytest.mark.parametrize(
    ("data_type", "order", "raw"),
    [
        ("int16", "AB", -321),
        ("int16", "BA", -321),
        ("uint16", "AB", 65530),
        ("uint16", "BA", 65530),
        ("int32", "ABCD", -1234567),
        ("int32", "CDAB", -1234567),
        ("int32", "BADC", -1234567),
        ("int32", "DCBA", -1234567),
        ("uint32", "ABCD", 4000000000),
        ("uint32", "CDAB", 4000000000),
        ("uint32", "BADC", 4000000000),
        ("uint32", "DCBA", 4000000000),
        ("float32", "ABCD", 42.5),
        ("float32", "CDAB", 42.5),
        ("float32", "BADC", 42.5),
        ("float32", "DCBA", 42.5),
    ],
)
def test_encode_decode_round_trip(data_type, order, raw):
    encoded = encode_raw(raw, data_type, order)
    decoded = decode_value(encoded, data_type, order)
    if data_type == "float32":
        assert decoded == pytest.approx(raw)
    else:
        assert decoded == raw


def test_encode_wire_layouts_match_documented_order():
    assert encode_raw(0x12345678, "uint32", "ABCD").hex() == "12345678"
    assert encode_raw(0x12345678, "uint32", "CDAB").hex() == "56781234"
    assert encode_raw(0x12345678, "uint32", "BADC").hex() == "34127856"
    assert encode_raw(0x12345678, "uint32", "DCBA").hex() == "78563412"
    assert encode_raw(0x1234, "uint16", "BA").hex() == "3412"


def test_decode_length_and_order_errors():
    with pytest.raises(CodecError) as length:
        decode_value(b"\x00", "uint16", "AB")
    assert length.value.code == "payload_length"
    with pytest.raises(CodecError) as order:
        decode_value(b"\x00\x01", "uint16", "ABCD")
    assert order.value.code == "unsupported_byte_order"
    with pytest.raises(CodecError) as float_length:
        decode_value(b"\x00\x01", "float32", "ABCD")
    assert float_length.value.code == "payload_length"


def test_non_finite_float_is_rejected():
    with pytest.raises(CodecError) as excinfo:
        decode_value(bytes.fromhex("7fc00000"), "float32", "ABCD")
    assert excinfo.value.code == "non_finite"
    with pytest.raises(CodecError):
        encode_raw(float("inf"), "float32", "ABCD")


def test_encode_rejects_fractional_integer():
    with pytest.raises(CodecError) as excinfo:
        encode_raw(1.25, "uint16", "AB")
    assert excinfo.value.code == "invalid_raw"


def test_scaling_and_range():
    assert apply_engineering(12340, 0.1, 0) == pytest.approx(1234.0)
    assert apply_engineering(225, 0.01, 1.5) == pytest.approx(3.75)
    assert classify_range(3000.0, 0, 3000) is None
    assert classify_range(3000.01, 0, 3000) == "overflow"
    assert classify_range(-0.1, 0, 3000) == "under_range"


def test_enum_mapping():
    assert enum_label({}, 3) == (True, None)
    assert enum_label({0: "idle", 1: "run"}, 1) == (True, "run")
    assert enum_label({0: "idle"}, 2) == (False, None)
    assert enum_label({0: "idle"}, 2.5) == (False, None)
