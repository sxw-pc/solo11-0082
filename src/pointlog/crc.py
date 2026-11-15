"""CRC-16/MODBUS, implemented without platform dependencies."""

from __future__ import annotations


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
