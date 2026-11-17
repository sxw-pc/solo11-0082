"""Deterministic register decoding for the supported data types and byte orders."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

DATA_TYPES = ("int16", "uint16", "int32", "uint32", "float32")
BYTE_ORDERS_16 = ("AB", "BA")
BYTE_ORDERS_32 = ("ABCD", "CDAB", "BADC", "DCBA")
ALL_BYTE_ORDERS = BYTE_ORDERS_16 + BYTE_ORDERS_32
REGISTER_COUNTS = {
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
}
SIGNED_TYPES = {"int16", "int32"}
WIDTHS = {"int16": 2, "uint16": 2, "int32": 4, "uint32": 4, "float32": 4}


class CodecError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _wire_to_big_endian(data: bytes, byte_order: str) -> bytes:
    if len(data) == 2:
        if byte_order == "AB":
            return data
        if byte_order == "BA":
            return data[::-1]
    if len(data) == 4:
        if byte_order == "ABCD":
            return data
        if byte_order == "CDAB":
            return data[2:4] + data[0:2]
        if byte_order == "BADC":
            return bytes((data[1], data[0], data[3], data[2]))
        if byte_order == "DCBA":
            return data[::-1]
    raise CodecError(
        "unsupported_byte_order", f"{byte_order} is not valid for {len(data)} bytes"
    )


def _big_endian_to_wire(data: bytes, byte_order: str) -> bytes:
    # All supported byte-order transforms are their own inverse.
    return _wire_to_big_endian(data, byte_order)


def decode_value(data: bytes, data_type: str, byte_order: str) -> int | float:
    """Decode register bytes, independent of host endianness."""
    if data_type not in REGISTER_COUNTS:
        raise CodecError("unsupported_data_type", f"unsupported type {data_type}")
    expected = REGISTER_COUNTS[data_type] * 2
    if len(data) != expected:
        raise CodecError(
            "payload_length",
            f"{data_type} needs {expected} bytes, got {len(data)}",
        )
    allowed = BYTE_ORDERS_16 if expected == 2 else BYTE_ORDERS_32
    if byte_order not in allowed:
        raise CodecError(
            "unsupported_byte_order", f"{byte_order} is not valid for {data_type}"
        )
    normalized = _wire_to_big_endian(data, byte_order)
    if data_type == "float32":
        value = struct.unpack(">f", normalized)[0]
        if not math.isfinite(value):
            raise CodecError("non_finite", "decoded float is not finite")
        return value
    return int.from_bytes(normalized, "big", signed=data_type in SIGNED_TYPES)


def encode_raw(raw: int | float, data_type: str, byte_order: str) -> bytes:
    """Encode a raw value back to wire register bytes (used by fixtures/tests)."""
    if data_type not in REGISTER_COUNTS:
        raise CodecError("unsupported_data_type", f"unsupported type {data_type}")
    if data_type == "float32":
        if not math.isfinite(float(raw)):
            raise CodecError("non_finite", "float raw value is not finite")
        normalized = struct.pack(">f", float(raw))
    else:
        if isinstance(raw, float) and not raw.is_integer():
            raise CodecError("invalid_raw", "integer type requires an integer raw value")
        normalized = int(raw).to_bytes(
            WIDTHS[data_type], "big", signed=data_type in SIGNED_TYPES
        )
    return _big_endian_to_wire(normalized, byte_order)


def apply_engineering(raw: int | float, scale: float, offset: float) -> float:
    value = float(raw) * float(scale) + float(offset)
    if not math.isfinite(value):
        raise CodecError("non_finite", "engineering value is not finite")
    return value


def classify_range(value: float, minimum: float, maximum: float) -> str | None:
    if value > maximum:
        return "overflow"
    if value < minimum:
        return "under_range"
    return None


def enum_label(mapping: dict[int, str], raw: int | float) -> tuple[bool, str | None]:
    """Return (in_enum, label). Empty mappings are treated as unconstrained."""
    if not mapping:
        return True, None
    if isinstance(raw, float) and not raw.is_integer():
        return False, None
    key = int(raw)
    return key in mapping, mapping.get(key)


@dataclass(frozen=True)
class ValueCheck:
    """Decoded value plus the outcome of the range/enum quality checks."""

    raw: int | float
    engineering: float
    range_code: str | None
    in_enum: bool
    label: str | None


def check_value(
    data: bytes,
    data_type: str,
    byte_order: str,
    scale: float,
    offset: float,
    range_min: float,
    range_max: float,
    enum: dict[int, str],
) -> ValueCheck:
    """Decode *data* and evaluate the range/enum quality checks in one call."""
    raw = decode_value(data, data_type, byte_order)
    engineering = apply_engineering(raw, scale, offset)
    range_code = classify_range(engineering, range_min, range_max)
    in_enum, label = enum_label(enum, raw)
    return ValueCheck(
        raw=raw,
        engineering=engineering,
        range_code=range_code,
        in_enum=in_enum,
        label=label,
    )
