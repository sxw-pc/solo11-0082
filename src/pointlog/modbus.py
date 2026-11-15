"""Modbus RTU-style frame parsing and construction for replay data."""

from __future__ import annotations

from dataclasses import dataclass

from .crc import append_crc, verify_crc

SUPPORTED_FUNCTIONS = (0x03, 0x04)
EXCEPTION_DESCRIPTIONS = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "device failure",
    5: "acknowledge",
    6: "device busy",
}


class FrameParseError(ValueError):
    """A structural frame problem with a stable machine-readable code."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RequestFrame:
    slave_id: int
    function: int
    start: int
    count: int
    crc_ok: bool
    raw: bytes

    @property
    def register_span(self) -> tuple[int, int]:
        return self.start, self.count


@dataclass(frozen=True)
class ResponseFrame:
    slave_id: int
    function: int
    byte_count: int
    data: bytes
    crc_ok: bool
    raw: bytes
    is_exception: bool = False
    exception_code: int | None = None


def hex_to_bytes(value: str) -> bytes:
    """Decode a hex string, allowing whitespace but rejecting odd lengths."""
    if not isinstance(value, str):
        raise FrameParseError("invalid_hex", "frame value is not a string")
    compact = "".join(value.split())
    if not compact:
        raise FrameParseError("empty_frame", "frame hex string is empty")
    if any(ch not in "0123456789abcdefABCDEF" for ch in compact):
        raise FrameParseError("invalid_hex", "frame contains non-hex characters")
    if len(compact) % 2:
        raise FrameParseError("odd_hex", "frame hex string has an odd number of chars")
    try:
        return bytes.fromhex(compact)
    except ValueError as exc:  # pragma: no cover - guarded above
        raise FrameParseError("invalid_hex", str(exc)) from exc


def parse_request(frame: bytes) -> RequestFrame:
    if len(frame) < 8:
        raise FrameParseError(
            "truncated_frame", f"request has {len(frame)} bytes, expected 8"
        )
    if len(frame) > 8:
        raise FrameParseError(
            "length_mismatch", f"request has {len(frame)} bytes, expected 8"
        )
    function = frame[1]
    if function not in SUPPORTED_FUNCTIONS:
        raise FrameParseError(
            "unsupported_function", f"unsupported request function 0x{function:02X}"
        )
    return RequestFrame(
        slave_id=frame[0],
        function=function,
        start=int.from_bytes(frame[2:4], "big"),
        count=int.from_bytes(frame[4:6], "big"),
        crc_ok=verify_crc(frame),
        raw=frame,
    )


def parse_response(frame: bytes) -> ResponseFrame:
    if len(frame) < 5:
        raise FrameParseError(
            "truncated_frame", f"response has {len(frame)} bytes, expected at least 5"
        )
    slave_id = frame[0]
    function = frame[1]
    if function & 0x80:
        if len(frame) != 5:
            raise FrameParseError(
                "length_mismatch",
                f"exception response has {len(frame)} bytes, expected 5",
            )
        return ResponseFrame(
            slave_id=slave_id,
            function=function,
            byte_count=0,
            data=b"",
            crc_ok=verify_crc(frame),
            raw=frame,
            is_exception=True,
            exception_code=frame[2],
        )

    byte_count = frame[2]
    expected_length = 5 + byte_count
    if len(frame) < expected_length:
        raise FrameParseError(
            "truncated_frame",
            f"response declares {byte_count} data bytes but frame is {len(frame)} bytes",
        )
    if len(frame) > expected_length:
        raise FrameParseError(
            "length_mismatch",
            f"response has trailing bytes: {len(frame)} vs expected {expected_length}",
        )
    if byte_count % 2:
        raise FrameParseError(
            "odd_byte_count", f"response data byte count is odd ({byte_count})"
        )
    return ResponseFrame(
        slave_id=slave_id,
        function=function,
        byte_count=byte_count,
        data=frame[3:-2],
        crc_ok=verify_crc(frame),
        raw=frame,
    )


def build_request(slave_id: int, function: int, start: int, count: int) -> bytes:
    if function not in SUPPORTED_FUNCTIONS:
        raise ValueError("only functions 0x03 and 0x04 are supported")
    payload = bytes((slave_id, function)) + start.to_bytes(2, "big") + count.to_bytes(
        2, "big"
    )
    return append_crc(payload)


def build_response(slave_id: int, function: int, data: bytes) -> bytes:
    if len(data) > 250:
        raise ValueError("response payload is too long")
    payload = bytes((slave_id, function, len(data))) + data
    return append_crc(payload)


def build_exception_response(
    slave_id: int, request_function: int, exception_code: int
) -> bytes:
    payload = bytes((slave_id, request_function | 0x80, exception_code))
    return append_crc(payload)


def corrupt_crc(frame: bytes) -> bytes:
    """Flip one CRC byte while preserving frame length."""
    if len(frame) < 2:
        raise ValueError("frame is too short to corrupt its CRC")
    changed = bytearray(frame)
    changed[-2] ^= 0x01
    return bytes(changed)


def exception_description(code: int) -> str:
    return EXCEPTION_DESCRIPTIONS.get(code, "unknown exception")
