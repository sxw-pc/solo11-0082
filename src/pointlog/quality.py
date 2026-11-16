"""Quality-code catalogue and the single deterministic decision order."""

from __future__ import annotations

from dataclasses import dataclass

GOOD = "good"
BAD_FRAME = "bad_frame"
BAD_CRC_REQUEST = "bad_crc_request"
BAD_CRC_RESPONSE = "bad_crc_response"
EXCEPTION_RESPONSE = "exception_response"
NO_CONTRACT = "no_contract"
MISSING_RESPONSE = "missing_response"
DUPLICATE = "duplicate"
DECODE_ERROR = "decode_error"
OVERFLOW = "overflow"
UNDER_RANGE = "under_range"
NOT_IN_ENUM = "not_in_enum"
PAIR_TIMEOUT = "pair_timeout"
PAIR_AMBIGUOUS = "pair_ambiguous"
UNPAIRED_EVENT = "unpaired_event"
DUPLICATE_PAIR_ID = "duplicate_pair_id"
PAIR_ORDER_INVERTED = "pair_order_inverted"

QUALITY_ORDER = (
    GOOD,
    BAD_FRAME,
    BAD_CRC_REQUEST,
    BAD_CRC_RESPONSE,
    EXCEPTION_RESPONSE,
    NO_CONTRACT,
    MISSING_RESPONSE,
    DUPLICATE,
    DECODE_ERROR,
    OVERFLOW,
    UNDER_RANGE,
    NOT_IN_ENUM,
    PAIR_TIMEOUT,
    PAIR_AMBIGUOUS,
    UNPAIRED_EVENT,
    DUPLICATE_PAIR_ID,
    PAIR_ORDER_INVERTED,
)

QUALITY_META: dict[str, dict[str, object]] = {
    GOOD: {
        "severity": "info",
        "pending_recollect": False,
        "description": "frame, CRC, range and enum checks all passed",
    },
    BAD_FRAME: {
        "severity": "error",
        "pending_recollect": True,
        "description": "request or response frame is malformed or structurally inconsistent",
    },
    BAD_CRC_REQUEST: {
        "severity": "error",
        "pending_recollect": True,
        "description": "request frame CRC does not match",
    },
    BAD_CRC_RESPONSE: {
        "severity": "error",
        "pending_recollect": True,
        "description": "response frame CRC does not match",
    },
    EXCEPTION_RESPONSE: {
        "severity": "error",
        "pending_recollect": True,
        "description": "device returned a Modbus-style exception response",
    },
    NO_CONTRACT: {
        "severity": "warning",
        "pending_recollect": False,
        "description": "request has no matching point contract",
    },
    MISSING_RESPONSE: {
        "severity": "warning",
        "pending_recollect": True,
        "description": "contract point has no usable response in the replay",
    },
    DUPLICATE: {
        "severity": "warning",
        "pending_recollect": False,
        "description": "same device/point/timestamp already appeared in this batch",
    },
    DECODE_ERROR: {
        "severity": "error",
        "pending_recollect": True,
        "description": "register payload could not be decoded deterministically",
    },
    OVERFLOW: {
        "severity": "warning",
        "pending_recollect": False,
        "description": "engineering value is above the configured range maximum",
    },
    UNDER_RANGE: {
        "severity": "warning",
        "pending_recollect": False,
        "description": "engineering value is below the configured range minimum",
    },
    NOT_IN_ENUM: {
        "severity": "warning",
        "pending_recollect": False,
        "description": "raw enum value is absent from the configured mapping",
    },
    PAIR_TIMEOUT: {
        "severity": "warning",
        "pending_recollect": True,
        "description": "split-frame request had no matching response within its "
                       "declared timeout",
    },
    PAIR_AMBIGUOUS: {
        "severity": "error",
        "pending_recollect": True,
        "description": "split-frame response matches more than one candidate "
                       "request; no inference is allowed",
    },
    UNPAIRED_EVENT: {
        "severity": "warning",
        "pending_recollect": True,
        "description": "split-frame event could not be paired under the active "
                       "pairing route",
    },
    DUPLICATE_PAIR_ID: {
        "severity": "error",
        "pending_recollect": True,
        "description": "explicit pair identifier reused within one channel",
    },
    PAIR_ORDER_INVERTED: {
        "severity": "error",
        "pending_recollect": True,
        "description": "paired response timestamp predates its request",
    },
}


@dataclass(frozen=True)
class QualityFlags:
    bad_frame: bool = False
    bad_crc_request: bool = False
    bad_crc_response: bool = False
    exception_response: bool = False
    no_contract: bool = False
    missing_response: bool = False
    duplicate: bool = False
    decode_error: bool = False
    overflow: bool = False
    under_range: bool = False
    not_in_enum: bool = False


def determine_quality(flags: QualityFlags) -> str:
    """Return exactly one quality code using the documented priority order.

    CRC failures outrank value range checks. An exception response outranks any
    decode error. Malformed frames and request CRC failures are evaluated before
    response-level checks because they prevent meaningful response interpretation.
    """
    if flags.bad_frame:
        return BAD_FRAME
    if flags.bad_crc_request:
        return BAD_CRC_REQUEST
    if flags.bad_crc_response:
        return BAD_CRC_RESPONSE
    if flags.exception_response:
        return EXCEPTION_RESPONSE
    if flags.no_contract:
        return NO_CONTRACT
    if flags.missing_response:
        return MISSING_RESPONSE
    if flags.duplicate:
        return DUPLICATE
    if flags.decode_error:
        return DECODE_ERROR
    if flags.overflow:
        return OVERFLOW
    if flags.under_range:
        return UNDER_RANGE
    if flags.not_in_enum:
        return NOT_IN_ENUM
    return GOOD


def is_quality_code(code: str) -> bool:
    return code in QUALITY_META


def severity(code: str) -> str:
    return str(QUALITY_META.get(code, {"severity": "error"})["severity"])


def is_pending(code: str) -> bool:
    return bool(QUALITY_META.get(code, {"pending_recollect": True})["pending_recollect"])


def quality_meta(code: str) -> dict[str, object]:
    return dict(QUALITY_META.get(code, {
        "severity": "error",
        "pending_recollect": True,
        "description": "unknown quality code",
    }))
