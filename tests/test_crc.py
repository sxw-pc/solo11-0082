import pytest

from pointlog.crc import append_crc, crc16_bytes, crc16_modbus, verify_crc


def test_known_modbus_vector():
    payload = bytes.fromhex("01030000000a")
    assert crc16_modbus(payload) == 0xCDC5
    assert crc16_bytes(payload) == bytes.fromhex("c5cd")
    framed = append_crc(payload)
    assert framed.hex() == "01030000000ac5cd"
    assert verify_crc(framed)


@pytest.mark.parametrize(
    "payload_hex",
    [
        "010300000001",
        "020400140002",
        "11 03 00 00 00 02",
        "ff",
        "0103000000000000",
    ],
)
def test_round_trip(payload_hex):
    payload = bytes.fromhex(payload_hex.replace(" ", ""))
    assert verify_crc(append_crc(payload))


def test_crc_changes_when_payload_changes():
    first = append_crc(bytes.fromhex("010300000001"))
    second = append_crc(bytes.fromhex("010300000002"))
    assert first[-2:] != second[-2:]


def test_verify_rejects_mutated_and_short_frames():
    framed = bytearray(append_crc(bytes.fromhex("010300000001")))
    framed[-1] ^= 0x80
    assert not verify_crc(bytes(framed))
    assert not verify_crc(b"")
    assert not verify_crc(b"\x01")
    assert not verify_crc(b"\x01\x03")


def test_crc_byte_order_is_low_first():
    payload = b"\x01\x03\x00\x00\x00\x01"
    value = crc16_modbus(payload)
    assert append_crc(payload)[-2:] == bytes((value & 0xFF, value >> 8))
