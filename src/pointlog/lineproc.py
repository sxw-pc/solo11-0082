"""Processing of one replay line into a frame, issues and an optional reading."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import codec
from .contract import ContractSet, PointContract
from .modbus import (
    FrameParseError,
    exception_description,
    hex_to_bytes,
    parse_request,
    parse_response,
)
from .quality import (
    BAD_CRC_REQUEST,
    BAD_CRC_RESPONSE,
    BAD_FRAME,
    DUPLICATE,
    GOOD,
    QualityFlags,
    determine_quality,
    is_pending,
    severity,
)
from .replay import ReplayLine
from .store import Store
from .util import frame_summary, normalize_number


@dataclass
class ProcessedLine:
    frame: dict[str, Any]
    issues: list[dict[str, Any]] = field(default_factory=list)
    reading: dict[str, Any] | None = None


def _base_frame(line: ReplayLine) -> dict[str, Any]:
    return {
        "line_no": line.line_no,
        "ts": line.ts,
        "source": line.source,
        "request_hex": line.request_hex,
        "response_hex": line.response_hex,
        "request_crc_ok": None,
        "response_crc_ok": None,
        "slave_id": None,
        "function_code": None,
        "start_address": None,
        "register_count": None,
        "device_id": "",
        "point_key": "",
        "contract_matched": 0,
        "quality_code": GOOD,
        "issue_code": None,
        "detail": "",
    }


def _issue(
    line: ReplayLine,
    *,
    issue_code: str,
    quality_code: str,
    summary: str,
    detail: str,
    point: PointContract | None = None,
    pending: bool | None = None,
) -> dict[str, Any]:
    return {
        "line_no": line.line_no,
        "device_id": point.device_id if point else "",
        "point_key": point.point_key if point else "",
        "issue_code": issue_code,
        "quality_code": quality_code,
        "severity": severity(quality_code),
        "frame_summary": frame_summary(summary),
        "detail": detail,
        "ts": line.ts,
        "pending_collect": int(is_pending(quality_code) if pending is None else pending),
    }


def _fail(
    frame: dict[str, Any],
    line: ReplayLine,
    *,
    issue_code: str,
    quality_code: str,
    summary: str,
    detail: str,
    point: PointContract | None = None,
    pending: bool | None = None,
) -> ProcessedLine:
    frame["quality_code"] = quality_code
    frame["issue_code"] = issue_code
    frame["detail"] = detail
    return ProcessedLine(
        frame=frame,
        issues=[_issue(line, issue_code=issue_code, quality_code=quality_code,
                       summary=summary, detail=detail, point=point, pending=pending)],
    )


def process_line(
    store: Store,
    batch_id: str,
    contract: ContractSet,
    line: ReplayLine,
    seen_points: set[str],
) -> ProcessedLine:
    frame = _base_frame(line)
    if not line.valid:
        return _fail(
            frame, line, issue_code=line.error_code or "invalid_line",
            quality_code=BAD_FRAME, summary=line.raw_text,
            detail=line.error_detail or "invalid replay line",
        )

    try:
        request_bytes = hex_to_bytes(line.request_hex)
        request = parse_request(request_bytes)
    except FrameParseError as exc:
        return _fail(
            frame, line, issue_code=exc.code, quality_code=BAD_FRAME,
            summary=line.request_hex, detail=f"request: {exc.detail}",
        )

    frame.update({
        "request_crc_ok": int(request.crc_ok),
        "slave_id": request.slave_id,
        "function_code": request.function,
        "start_address": request.start,
        "register_count": request.count,
    })
    point = contract.find(
        request.slave_id, request.function, request.start, request.count
    )
    if point is not None:
        frame["device_id"] = point.device_id
        frame["point_key"] = point.point_key
        frame["contract_matched"] = 1
        seen_points.add(point.point_key)

    if not request.crc_ok:
        return _fail(
            frame, line, issue_code=BAD_CRC_REQUEST, quality_code=BAD_CRC_REQUEST,
            summary=line.request_hex,
            detail="request CRC check failed", point=point,
        )
    if point is None:
        detail = (
            f"no contract for slave={request.slave_id} function={request.function} "
            f"start={request.start} count={request.count}"
        )
        return _fail(
            frame, line, issue_code="no_contract", quality_code="no_contract",
            summary=line.request_hex, detail=detail, pending=False,
        )
    if not line.response_hex.strip():
        return _fail(
            frame, line, issue_code="missing_response", quality_code="missing_response",
            summary=line.request_hex, detail="response_hex is empty", point=point,
        )

    try:
        response_bytes = hex_to_bytes(line.response_hex)
        response = parse_response(response_bytes)
    except FrameParseError as exc:
        return _fail(
            frame, line, issue_code=exc.code, quality_code=BAD_FRAME,
            summary=line.response_hex, detail=f"response: {exc.detail}", point=point,
        )
    frame["response_crc_ok"] = int(response.crc_ok)

    if not response.crc_ok:
        return _fail(
            frame, line, issue_code=BAD_CRC_RESPONSE, quality_code=BAD_CRC_RESPONSE,
            summary=line.response_hex, detail="response CRC check failed", point=point,
        )
    if response.slave_id != request.slave_id:
        return _fail(
            frame, line, issue_code="slave_mismatch", quality_code=BAD_FRAME,
            summary=line.response_hex,
            detail=f"response slave {response.slave_id} != request slave {request.slave_id}",
            point=point,
        )
    expected_function = request.function | 0x80 if response.is_exception else request.function
    if response.function != expected_function:
        return _fail(
            frame, line, issue_code="function_mismatch", quality_code=BAD_FRAME,
            summary=line.response_hex,
            detail=f"response function 0x{response.function:02X} != "
                   f"expected 0x{expected_function:02X}",
            point=point,
        )
    if response.is_exception:
        code = int(response.exception_code or 0)
        return _fail(
            frame, line, issue_code="exception_response", quality_code="exception_response",
            summary=line.response_hex,
            detail=f"exception code {code}: {exception_description(code)}", point=point,
        )
    expected_bytes = point.count * 2
    if response.byte_count != expected_bytes:
        return _fail(
            frame, line, issue_code="byte_count_mismatch", quality_code=BAD_FRAME,
            summary=line.response_hex,
            detail=f"response byte count {response.byte_count} != expected {expected_bytes}",
            point=point,
        )

    try:
        raw_value = codec.decode_value(response.data, point.data_type, point.byte_order)
        engineering = codec.apply_engineering(raw_value, point.scale, point.offset)
    except codec.CodecError as exc:
        return _fail(
            frame, line, issue_code=exc.code, quality_code="decode_error",
            summary=line.response_hex, detail=f"decode failed: {exc.detail}", point=point,
        )

    engineering = normalize_number(engineering)
    range_code = codec.classify_range(engineering, point.range_min, point.range_max)
    in_enum, label = codec.enum_label(point.enum, raw_value)
    duplicate = store.reading_exists(
        batch_id, point.device_id, point.point_key, line.ts
    )
    quality = determine_quality(QualityFlags(
        duplicate=duplicate,
        overflow=range_code == "overflow",
        under_range=range_code == "under_range",
        not_in_enum=not in_enum,
    ))
    frame["quality_code"] = quality
    if quality != GOOD:
        frame["issue_code"] = quality
        frame["detail"] = _quality_detail(quality, engineering, point)
        pending = is_pending(quality)
        issues = [_issue(
            line, issue_code=quality, quality_code=quality,
            summary=line.response_hex, detail=frame["detail"],
            point=point, pending=pending,
        )]
    else:
        issues = []

    reading = None
    if quality != DUPLICATE:
        reading = {
            "device_id": point.device_id,
            "point_key": point.point_key,
            "ts": line.ts,
            "line_no": line.line_no,
            "raw_value": raw_value,
            "engineering_value": float(engineering),
            "unit": point.unit,
            "quality_code": quality,
            "enum_label": label,
            "source": line.source,
        }
    return ProcessedLine(frame=frame, issues=issues, reading=reading)


def _quality_detail(quality: str, engineering: float, point: PointContract) -> str:
    if quality == "overflow":
        return f"engineering {engineering} > range maximum {point.range_max}"
    if quality == "under_range":
        return f"engineering {engineering} < range minimum {point.range_min}"
    if quality == "not_in_enum":
        return f"raw value is absent from enum mapping for {point.point_key}"
    if quality == DUPLICATE:
        return (
            f"duplicate device={point.device_id} point={point.point_key} "
            "timestamp already stored in this batch"
        )
    return quality
