"""Processing of one replay line into a frame, issues and an optional reading."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import codec
from .contract import ContractSet, PointContract
from .modbus import (
    FrameParseError,
    RequestFrame,
    ResponseFrame,
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
    DUPLICATE_PAIR_ID,
    GOOD,
    PAIR_AMBIGUOUS,
    PAIR_ORDER_INVERTED,
    PAIR_TIMEOUT,
    UNPAIRED_EVENT,
    QualityFlags,
    determine_quality,
    is_pending,
    severity,
)
from .replay import ReplayEvent, ReplayLine, parse_event_ts
from .store import Store
from .util import frame_summary, normalize_number, sha256_text


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


# ---------------------------------------------------------------------------
# Split-frame (separate request/response event) pairing
# ---------------------------------------------------------------------------

PAIRING_ROUTES = ("explicit", "single_in_flight")


@dataclass
class EventOutcome:
    """Everything one replay event (or the EOF flush) decided, for one transaction."""

    event_row: dict[str, Any] | None = None
    event_updates: list[dict[str, Any]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    reading: dict[str, Any] | None = None


@dataclass
class _OpenRequest:
    channel: str
    line_no: int
    dt: datetime
    ts: str
    frame_hex: str
    request: RequestFrame
    point: PointContract
    pair_id: str
    timeout_ms: int
    event_sha256: str
    source: str

    @property
    def deadline(self) -> datetime:
        return self.dt + timedelta(milliseconds=self.timeout_ms)


@dataclass
class _ChannelState:
    open_requests: list[_OpenRequest] = field(default_factory=list)
    # Timed-out requests stay as late candidates for one extra timeout window
    # so a late response is recognised as evidence instead of being guessed.
    late_requests: list[tuple[_OpenRequest, datetime]] = field(default_factory=list)
    pair_ids: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_dt: datetime | None = None


def _compatible(request: RequestFrame, response: ResponseFrame) -> bool:
    """True when the response bytes could answer the request (Modbus RTU has
    no transaction id, so slave/function/register-length are all we have)."""
    if request.slave_id != response.slave_id:
        return False
    if response.is_exception:
        return response.function == (request.function | 0x80)
    return (
        response.function == request.function
        and response.byte_count == request.count * 2
    )


def _anchor_line(frame: dict[str, Any]) -> ReplayLine:
    return ReplayLine(
        line_no=int(frame["line_no"]),
        raw_text="",
        ts=str(frame["ts"]),
        request_hex=str(frame.get("request_hex", "")),
        response_hex=str(frame.get("response_hex", "")),
        source=str(frame.get("source", "")),
    )


class EventPairer:
    """Deterministic per-channel pairing state machine for split-frame replays.

    Two business-authorised routes exist:

    - ``explicit``: pair only events that carry the same ``pair_id`` on the
      same channel; every other event stays unresolved evidence.
    - ``single_in_flight``: pair a response with the only compatible open
      request on its channel, and only when no timed-out request could be
      its source instead. Two concurrent candidates or a late-response
      ambiguity always become ledger evidence, never a guessed reading.

    The machine is a pure function of the event stream, so replaying the
    already-committed prefix after an interruption rebuilds the exact state
    (a request that timed out before the interruption is still timed out).
    """

    def __init__(
        self,
        *,
        store: Store,
        batch_id: str,
        contract: ContractSet,
        route: str,
        pair_timeout_ms: int,
        seen_points: set[str],
    ):
        if route not in PAIRING_ROUTES:
            raise ValueError(f"pairing route must be one of {PAIRING_ROUTES}")
        if pair_timeout_ms <= 0:
            raise ValueError("pair_timeout_ms must be a positive integer")
        self._store = store
        self._batch_id = batch_id
        self._contract = contract
        self._route = route
        self._default_timeout_ms = pair_timeout_ms
        self._seen_points = seen_points
        self._channels: dict[str, _ChannelState] = {}

    # -- public API ---------------------------------------------------------

    def handle_event(self, event: ReplayEvent) -> EventOutcome:
        outcome = EventOutcome(event_row=self._event_row(event))
        if not event.valid:
            frame = self._event_frame(event)
            self._fail(
                outcome, frame,
                issue_code=event.error_code or "invalid_line",
                quality_code=BAD_FRAME,
                summary=event.raw_text,
                detail=event.error_detail or "invalid replay event",
            )
            outcome.event_row["outcome"] = "invalid"
            return outcome
        dt = parse_event_ts(event.ts)
        channel = self._channels.setdefault(event.channel, _ChannelState())
        self._expire(channel, dt, outcome)
        if event.direction == "request":
            self._handle_request(event, dt, channel, outcome)
        else:
            self._handle_response(event, dt, channel, outcome)
        channel.last_dt = dt
        return outcome

    def finalize(self) -> EventOutcome:
        """End-of-recording flush: every still-open request becomes evidence."""
        outcome = EventOutcome(event_row=None)
        for channel in self._channels.values():
            for request in list(channel.open_requests):
                if channel.last_dt is not None and channel.last_dt > request.deadline:
                    issue_code = "pair_timeout"
                    detail = (
                        f"no response within declared timeout of "
                        f"{request.timeout_ms} ms"
                    )
                else:
                    issue_code = "pair_incomplete"
                    detail = "recording ended before the declared timeout elapsed"
                self._timeout_request(
                    outcome, channel, request, issue_code=issue_code, detail=detail
                )
            channel.open_requests = []
            channel.late_requests = []
        return outcome

    # -- event/frame helpers --------------------------------------------------

    def _event_row(self, event: ReplayEvent) -> dict[str, Any]:
        return {
            "line_no": event.line_no,
            "ts": event.ts,
            "channel": event.channel,
            "direction": event.direction,
            "frame_hex": event.frame_hex,
            "pair_id": event.pair_id,
            "timeout_ms": event.timeout_ms or 0,
            "event_sha256": sha256_text(event.raw_text),
            "outcome": "open",
            "paired_line_no": None,
            "detail": "",
        }

    def _event_frame(self, event: ReplayEvent, **overrides: Any) -> dict[str, Any]:
        frame = {
            "line_no": event.line_no,
            "ts": event.ts,
            "source": event.source or event.channel,
            "request_hex": "",
            "response_hex": "",
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
            "channel": event.channel,
            "direction": event.direction,
            "pair_id": event.pair_id,
            "pair_route": self._route,
            "request_line_no": None,
            "response_line_no": None,
            "request_sha256": "",
            "response_sha256": "",
        }
        frame.update(overrides)
        return frame

    def _request_frame(self, request: _OpenRequest) -> dict[str, Any]:
        return {
            "line_no": request.line_no,
            "ts": request.ts,
            "source": request.source,
            "request_hex": request.frame_hex,
            "response_hex": "",
            "request_crc_ok": int(request.request.crc_ok),
            "response_crc_ok": None,
            "slave_id": request.request.slave_id,
            "function_code": request.request.function,
            "start_address": request.request.start,
            "register_count": request.request.count,
            "device_id": request.point.device_id,
            "point_key": request.point.point_key,
            "contract_matched": 1,
            "quality_code": GOOD,
            "issue_code": None,
            "detail": "",
            "channel": request.channel,
            "direction": "request",
            "pair_id": request.pair_id,
            "pair_route": self._route,
            "request_line_no": request.line_no,
            "response_line_no": None,
            "request_sha256": request.event_sha256,
            "response_sha256": "",
        }

    def _fail(
        self,
        outcome: EventOutcome,
        frame: dict[str, Any],
        *,
        issue_code: str,
        quality_code: str,
        summary: str,
        detail: str,
        point: PointContract | None = None,
        pending: bool | None = None,
    ) -> None:
        frame["quality_code"] = quality_code
        frame["issue_code"] = issue_code
        frame["detail"] = detail
        outcome.frames.append(frame)
        outcome.issues.append(_issue(
            _anchor_line(frame),
            issue_code=issue_code,
            quality_code=quality_code,
            summary=summary,
            detail=detail,
            point=point,
            pending=pending,
        ))

    # -- request side ---------------------------------------------------------

    def _handle_request(
        self,
        event: ReplayEvent,
        dt: datetime,
        channel: _ChannelState,
        outcome: EventOutcome,
    ) -> None:
        event_hash = sha256_text(event.raw_text)
        frame = self._event_frame(
            event,
            request_hex=event.frame_hex,
            request_line_no=event.line_no,
            request_sha256=event_hash,
        )
        try:
            request = parse_request(hex_to_bytes(event.frame_hex))
        except FrameParseError as exc:
            self._fail(outcome, frame, issue_code=exc.code, quality_code=BAD_FRAME,
                       summary=event.frame_hex, detail=f"request: {exc.detail}")
            outcome.event_row["outcome"] = "invalid_frame"
            return
        frame.update({
            "request_crc_ok": int(request.crc_ok),
            "slave_id": request.slave_id,
            "function_code": request.function,
            "start_address": request.start,
            "register_count": request.count,
        })
        point = self._contract.find(
            request.slave_id, request.function, request.start, request.count
        )
        if point is not None:
            frame["device_id"] = point.device_id
            frame["point_key"] = point.point_key
            frame["contract_matched"] = 1
            self._seen_points.add(point.point_key)
        if not request.crc_ok:
            self._fail(outcome, frame, issue_code=BAD_CRC_REQUEST,
                       quality_code=BAD_CRC_REQUEST, summary=event.frame_hex,
                       detail="request CRC check failed", point=point)
            outcome.event_row["outcome"] = "bad_crc"
            return
        if point is None:
            detail = (
                f"no contract for slave={request.slave_id} function={request.function} "
                f"start={request.start} count={request.count}"
            )
            self._fail(outcome, frame, issue_code="no_contract",
                       quality_code="no_contract", summary=event.frame_hex,
                       detail=detail, pending=False)
            outcome.event_row["outcome"] = "no_contract"
            return
        if event.pair_id and event.pair_id in channel.pair_ids:
            first = channel.pair_ids[event.pair_id]["request"]
            same = "identical" if first.frame_hex == event.frame_hex else "different"
            detail = (
                f"pair_id {event.pair_id!r} already used by request at line "
                f"{first.line_no} on channel {event.channel!r} ({same} request frame)"
            )
            self._fail(outcome, frame, issue_code="duplicate_pair_id",
                       quality_code=DUPLICATE_PAIR_ID, summary=event.frame_hex,
                       detail=detail, point=point)
            outcome.event_row["outcome"] = "duplicate_pair_id"
            return
        if self._route == "explicit" and not event.pair_id:
            self._fail(outcome, frame, issue_code="unpaired_event",
                       quality_code=UNPAIRED_EVENT, summary=event.frame_hex,
                       detail="explicit pairing route requires pair_id on every event",
                       point=point)
            outcome.event_row["outcome"] = "unpaired"
            return
        tracked = _OpenRequest(
            channel=event.channel,
            line_no=event.line_no,
            dt=dt,
            ts=event.ts,
            frame_hex=event.frame_hex,
            request=request,
            point=point,
            pair_id=event.pair_id,
            timeout_ms=event.timeout_ms or self._default_timeout_ms,
            event_sha256=event_hash,
            source=event.source or event.channel,
        )
        channel.open_requests.append(tracked)
        if event.pair_id:
            channel.pair_ids[event.pair_id] = {"request": tracked, "status": "open"}
        outcome.event_row["outcome"] = "open"

    # -- response side ----------------------------------------------------------

    def _handle_response(
        self,
        event: ReplayEvent,
        dt: datetime,
        channel: _ChannelState,
        outcome: EventOutcome,
    ) -> None:
        frame = self._event_frame(
            event,
            response_hex=event.frame_hex,
            response_line_no=event.line_no,
            response_sha256=sha256_text(event.raw_text),
        )
        try:
            response = parse_response(hex_to_bytes(event.frame_hex))
        except FrameParseError as exc:
            self._fail(outcome, frame, issue_code=exc.code, quality_code=BAD_FRAME,
                       summary=event.frame_hex, detail=f"response: {exc.detail}")
            outcome.event_row["outcome"] = "invalid_frame"
            return
        frame["response_crc_ok"] = int(response.crc_ok)
        frame["slave_id"] = response.slave_id
        frame["function_code"] = response.function
        if self._route == "explicit":
            self._response_explicit(event, dt, channel, response, frame, outcome)
        else:
            self._response_single_in_flight(event, channel, response, frame, outcome)

    def _response_explicit(
        self,
        event: ReplayEvent,
        dt: datetime,
        channel: _ChannelState,
        response: ResponseFrame,
        frame: dict[str, Any],
        outcome: EventOutcome,
    ) -> None:
        if not event.pair_id:
            self._fail(outcome, frame, issue_code="unpaired_event",
                       quality_code=UNPAIRED_EVENT, summary=event.frame_hex,
                       detail="explicit pairing route requires pair_id on every event")
            outcome.event_row["outcome"] = "unpaired"
            return
        entry = channel.pair_ids.get(event.pair_id)
        if entry is None:
            self._fail(
                outcome, frame, issue_code="unpaired_event",
                quality_code=UNPAIRED_EVENT, summary=event.frame_hex,
                detail=f"no request with pair_id {event.pair_id!r} on channel "
                       f"{event.channel!r}",
            )
            outcome.event_row["outcome"] = "unpaired"
            return
        request: _OpenRequest = entry["request"]
        frame.update({
            "request_hex": request.frame_hex,
            "request_crc_ok": int(request.request.crc_ok),
            "start_address": request.request.start,
            "register_count": request.request.count,
            "device_id": request.point.device_id,
            "point_key": request.point.point_key,
            "contract_matched": 1,
            "request_line_no": request.line_no,
            "request_sha256": request.event_sha256,
        })
        if entry["status"] == "timeout":
            self._fail(
                outcome, frame, issue_code="pair_timeout",
                quality_code=PAIR_TIMEOUT, summary=event.frame_hex,
                detail=f"response arrived after the declared timeout of "
                       f"{request.timeout_ms} ms (request line {request.line_no})",
                point=request.point,
            )
            outcome.event_row["outcome"] = "late"
            return
        if entry["status"] != "open":
            self._fail(
                outcome, frame, issue_code="unpaired_event",
                quality_code=UNPAIRED_EVENT, summary=event.frame_hex,
                detail=f"pair_id {event.pair_id!r} is already {entry['status']} "
                       f"(request line {request.line_no})",
                point=request.point,
            )
            outcome.event_row["outcome"] = "unpaired"
            return
        if dt < request.dt:
            self._fail(
                outcome, frame, issue_code="pair_order_inverted",
                quality_code=PAIR_ORDER_INVERTED, summary=event.frame_hex,
                detail=f"response ts {event.ts} predates request ts {request.ts} "
                       f"for pair_id {event.pair_id!r}",
                point=request.point,
            )
            outcome.event_row["outcome"] = "rejected"
            return
        self._pair(channel, request, event, outcome)

    def _response_single_in_flight(
        self,
        event: ReplayEvent,
        channel: _ChannelState,
        response: ResponseFrame,
        frame: dict[str, Any],
        outcome: EventOutcome,
    ) -> None:
        open_candidates = [
            request for request in channel.open_requests
            if _compatible(request.request, response)
        ]
        late_candidates = [
            request for request, until in channel.late_requests
            if _compatible(request.request, response)
        ]
        if not open_candidates and not late_candidates:
            self._fail(outcome, frame, issue_code="orphan_response",
                       quality_code=UNPAIRED_EVENT, summary=event.frame_hex,
                       detail="no open or timed-out request on channel "
                              f"{event.channel!r} matches this response")
            outcome.event_row["outcome"] = "orphan"
            return
        if len(open_candidates) == 1 and not late_candidates:
            self._pair(channel, open_candidates[0], event, outcome)
            return
        if not open_candidates and len(late_candidates) == 1:
            request = late_candidates[0]
            self._drop_late(channel, {request.line_no})
            frame.update({
                "request_hex": request.frame_hex,
                "request_crc_ok": int(request.request.crc_ok),
                "start_address": request.request.start,
                "register_count": request.request.count,
                "device_id": request.point.device_id,
                "point_key": request.point.point_key,
                "contract_matched": 1,
                "request_line_no": request.line_no,
                "request_sha256": request.event_sha256,
            })
            self._fail(
                outcome, frame, issue_code="late_response",
                quality_code=PAIR_TIMEOUT, summary=event.frame_hex,
                detail=f"response arrived after the declared timeout of request "
                       f"at line {request.line_no}; it is evidence, not a sample",
                point=request.point,
            )
            outcome.event_row["outcome"] = "late"
            return
        candidates = open_candidates + late_candidates
        self._drop_late(channel, {request.line_no for request in late_candidates})
        listing = ", ".join(
            f"line {request.line_no} (sha256 {request.event_sha256[:12]}…)"
            for request in candidates
        )
        detail = (
            f"response is byte-indistinguishable across {len(candidates)} candidate "
            f"requests ({listing}); neither most-recent nor FIFO guessing is "
            "permitted, so no reading was produced"
        )
        self._fail(outcome, frame, issue_code="pair_ambiguous",
                   quality_code=PAIR_AMBIGUOUS, summary=event.frame_hex,
                   detail=detail, point=candidates[0].point)
        outcome.event_row["outcome"] = "ambiguous"

    # -- pairing mechanics ------------------------------------------------------

    def _pair(
        self,
        channel: _ChannelState,
        request: _OpenRequest,
        event: ReplayEvent,
        outcome: EventOutcome,
    ) -> None:
        self._consume(channel, request, "paired")
        line = ReplayLine(
            line_no=event.line_no,
            raw_text=event.raw_text,
            ts=event.ts,
            request_hex=request.frame_hex,
            response_hex=event.frame_hex,
            source=event.source or request.source,
        )
        processed = process_line(
            self._store, self._batch_id, self._contract, line, self._seen_points
        )
        frame = dict(processed.frame)
        frame.update({
            "channel": request.channel,
            "direction": "response",
            "pair_id": request.pair_id or event.pair_id,
            "pair_route": self._route,
            "request_line_no": request.line_no,
            "response_line_no": event.line_no,
            "request_sha256": request.event_sha256,
            "response_sha256": sha256_text(event.raw_text),
        })
        outcome.frames.append(frame)
        outcome.issues.extend(processed.issues)
        outcome.reading = processed.reading
        outcome.event_row["outcome"] = "paired"
        outcome.event_row["paired_line_no"] = request.line_no
        outcome.event_updates.append({
            "line_no": request.line_no, "outcome": "paired",
            "paired_line_no": event.line_no,
        })

    def _expire(
        self, channel: _ChannelState, dt: datetime, outcome: EventOutcome
    ) -> None:
        still_open: list[_OpenRequest] = []
        for request in channel.open_requests:
            if request.deadline < dt:
                channel.late_requests.append(
                    (request, request.deadline + timedelta(milliseconds=request.timeout_ms))
                )
                self._timeout_request(
                    outcome, channel, request, issue_code="pair_timeout",
                    detail=f"no response within declared timeout of "
                           f"{request.timeout_ms} ms",
                )
            else:
                still_open.append(request)
        channel.open_requests = still_open
        channel.late_requests = [
            (request, until)
            for request, until in channel.late_requests
            if until >= dt
        ]

    def _timeout_request(
        self,
        outcome: EventOutcome,
        channel: _ChannelState,
        request: _OpenRequest,
        *,
        issue_code: str,
        detail: str,
    ) -> None:
        frame = self._request_frame(request)
        self._fail(outcome, frame, issue_code=issue_code, quality_code=PAIR_TIMEOUT,
                   summary=request.frame_hex, detail=detail, point=request.point)
        outcome.event_updates.append({
            "line_no": request.line_no, "outcome": "timeout", "paired_line_no": None,
        })
        if request.pair_id and request.pair_id in channel.pair_ids:
            channel.pair_ids[request.pair_id]["status"] = "timeout"

    @staticmethod
    def _consume(channel: _ChannelState, request: _OpenRequest, status: str) -> None:
        channel.open_requests = [
            item for item in channel.open_requests if item.line_no != request.line_no
        ]
        if request.pair_id and request.pair_id in channel.pair_ids:
            channel.pair_ids[request.pair_id]["status"] = status

    @staticmethod
    def _drop_late(channel: _ChannelState, line_numbers: set[int]) -> None:
        channel.late_requests = [
            (request, until)
            for request, until in channel.late_requests
            if request.line_no not in line_numbers
        ]
