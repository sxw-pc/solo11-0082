"""CRC-16/MODBUS, implemented without platform dependencies."""

from __future__ import annotations

from collections.abc import Iterable


def crc16_modbus(data: bytes) -> int:
    """Return the integer CRC-16/MODBUS value for *data*.

    Polynomial 0xA001 (reflected 0x8005), initial value 0xFFFF, no final XOR.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def crc16_bytes(data: bytes) -> bytes:
    """Return CRC bytes in Modbus wire order (low byte first)."""
    value = crc16_modbus(data)
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def append_crc(payload: bytes) -> bytes:
    return payload + crc16_bytes(payload)


def verify_crc(frame: bytes) -> bool:
    """Check the trailing low/high CRC bytes of a complete frame."""
    if len(frame) < 3:
        return False
    return frame[-2:] == crc16_bytes(frame[:-2])


def flip_bits(frame: bytes, bits: Iterable[int]) -> bytes:
    """Return a copy of *frame* with the given bit positions flipped.

    Bit numbering follows the audit convention: bit 0 is the least
    significant bit of byte 0, and bit ``i`` is bit ``i % 8`` of byte
    ``i // 8`` counted from the start of the frame.
    """
    changed = bytearray(frame)
    width = len(frame) * 8
    for bit in bits:
        if not isinstance(bit, int) or isinstance(bit, bool) or not 0 <= bit < width:
            raise ValueError(f"bit position {bit!r} outside {width}-bit frame")
        changed[bit // 8] ^= 1 << (bit % 8)
    return bytes(changed)


def crc_bit_syndromes(frame: bytes) -> tuple[int, ...]:
    """Return the CRC syndrome of every bit position of *frame*.

    *frame* must carry a valid trailing CRC (see :func:`verify_crc`). XOR-ing
    the syndromes of a set of flipped bit positions yields zero exactly when
    the mutated frame still passes :func:`verify_crc`: payload-bit syndromes
    capture the change in the computed CRC, CRC-field syndromes the change in
    the stored CRC, and the two must cancel for the check to keep passing.
    """
    if len(frame) < 3:
        raise ValueError("frame is too short to carry a CRC")
    if not verify_crc(frame):
        raise ValueError("frame CRC is invalid; syndromes need a valid baseline")
    payload = frame[:-2]
    base = crc16_modbus(payload)
    syndromes: list[int] = []
    for bit in range(len(payload) * 8):
        mutated = bytearray(payload)
        mutated[bit // 8] ^= 1 << (bit % 8)
        syndromes.append(crc16_modbus(bytes(mutated)) ^ base)
    syndromes.extend(1 << bit for bit in range(16))
    return tuple(syndromes)
