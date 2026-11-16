"""JSONL replay-file fingerprinting and line iteration."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .util import sha256_file


class ReplayError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ReplayLine:
    line_no: int
    raw_text: str
    ts: str
    request_hex: str
    response_hex: str
    source: str
    error_code: str | None = None
    error_detail: str = ""

    @property
    def valid(self) -> bool:
        return self.error_code is None


@dataclass(frozen=True)
class ReplaySource:
    path: Path
    sha256: str
    total_lines: int

    def iter_lines(self, skip_lines: int = 0) -> Iterator[ReplayLine]:
        if skip_lines < 0:
            raise ReplayError("invalid_offset", "skip_lines must not be negative")
        try:
            with self.path.open("r", encoding="utf-8", newline=None) as handle:
                for line_no, raw in enumerate(handle, start=1):
                    if line_no <= skip_lines:
                        continue
                    yield parse_line(line_no, raw)
        except OSError as exc:
            raise ReplayError("replay_read", f"cannot read replay file: {exc}") from exc

    def iter_events(self, skip_lines: int = 0) -> Iterator[ReplayEvent]:
        """Iterate split-frame events; same line order and offsets as iter_lines."""
        if skip_lines < 0:
            raise ReplayError("invalid_offset", "skip_lines must not be negative")
        try:
            with self.path.open("r", encoding="utf-8", newline=None) as handle:
                for line_no, raw in enumerate(handle, start=1):
                    if line_no <= skip_lines:
                        continue
                    yield parse_event_line(line_no, raw)
        except OSError as exc:
            raise ReplayError("replay_read", f"cannot read replay file: {exc}") from exc


def parse_line(line_no: int, raw: str) -> ReplayLine:
    text = raw.rstrip("\r\n")
    if not text.strip():
        return ReplayLine(
            line_no, text, "", "", "", "", "invalid_line", "line is blank"
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return ReplayLine(
            line_no, text, "", "", "", "", "invalid_json", f"invalid JSON: {exc.msg}"
        )
    if not isinstance(value, dict):
        return ReplayLine(
            line_no, text, "", "", "", "", "invalid_line", "line must be a JSON object"
        )

    ts = value.get("ts")
    request_hex = value.get("request_hex")
    response_hex = value.get("response_hex")
    source = value.get("source")
    errors: list[str] = []
    if not isinstance(ts, str) or not ts.strip():
        errors.append("ts must be a non-empty string")
    if not isinstance(request_hex, str) or not request_hex.strip():
        errors.append("request_hex must be a non-empty string")
    if not isinstance(response_hex, str):
        errors.append("response_hex must be a string (empty is allowed)")
    if not isinstance(source, str) or not source.strip():
        errors.append("source must be a non-empty string")
    if errors:
        return ReplayLine(
            line_no,
            text,
            ts if isinstance(ts, str) else "",
            request_hex if isinstance(request_hex, str) else "",
            response_hex if isinstance(response_hex, str) else "",
            source if isinstance(source, str) else "",
            "invalid_line",
            "; ".join(errors),
        )
    return ReplayLine(
        line_no=line_no,
        raw_text=text,
        ts=ts.strip(),
        request_hex=request_hex,
        response_hex=response_hex,
        source=source.strip(),
    )


EVENT_DIRECTIONS = ("request", "response")


@dataclass(frozen=True)
class ReplayEvent:
    """One split-frame recording event (a single request or response frame)."""

    line_no: int
    raw_text: str
    ts: str
    channel: str
    direction: str
    frame_hex: str
    pair_id: str
    timeout_ms: int | None
    source: str
    error_code: str | None = None
    error_detail: str = ""

    @property
    def valid(self) -> bool:
        return self.error_code is None


def parse_event_ts(text: str) -> datetime:
    """Parse an ISO-8601 timestamp as UTC; naive values are treated as UTC."""
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReplayError("invalid_ts", f"ts is not an ISO-8601 timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_event_line(line_no: int, raw: str) -> ReplayEvent:
    text = raw.rstrip("\r\n")

    def _invalid(code: str, detail: str, value: dict | None = None) -> ReplayEvent:
        value = value or {}
        return ReplayEvent(
            line_no=line_no,
            raw_text=text,
            ts=value.get("ts") if isinstance(value.get("ts"), str) else "",
            channel=value.get("channel") if isinstance(value.get("channel"), str) else "",
            direction=(
                value.get("direction") if isinstance(value.get("direction"), str) else ""
            ),
            frame_hex=(
                value.get("frame_hex") if isinstance(value.get("frame_hex"), str) else ""
            ),
            pair_id=value.get("pair_id") if isinstance(value.get("pair_id"), str) else "",
            timeout_ms=(
                value.get("timeout_ms") if isinstance(value.get("timeout_ms"), int)
                and not isinstance(value.get("timeout_ms"), bool) else None
            ),
            source=value.get("source") if isinstance(value.get("source"), str) else "",
            error_code=code,
            error_detail=detail,
        )

    if not text.strip():
        return _invalid("invalid_line", "line is blank")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return _invalid("invalid_json", f"invalid JSON: {exc.msg}")
    if not isinstance(value, dict):
        return _invalid("invalid_line", "line must be a JSON object")

    errors: list[str] = []
    ts = value.get("ts")
    if not isinstance(ts, str) or not ts.strip():
        errors.append("ts must be a non-empty string")
    else:
        try:
            parse_event_ts(ts)
        except ReplayError as exc:
            errors.append(exc.detail)
    channel = value.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        errors.append("channel must be a non-empty string")
    direction = value.get("direction")
    if direction not in EVENT_DIRECTIONS:
        errors.append("direction must be 'request' or 'response'")
    frame_hex = value.get("frame_hex")
    if not isinstance(frame_hex, str) or not frame_hex.strip():
        errors.append("frame_hex must be a non-empty string")
    pair_id = value.get("pair_id")
    if pair_id is not None and (not isinstance(pair_id, str) or not pair_id.strip()):
        errors.append("pair_id must be a non-empty string when present")
    timeout_ms = value.get("timeout_ms")
    if timeout_ms is not None and (
        not isinstance(timeout_ms, int)
        or isinstance(timeout_ms, bool)
        or timeout_ms <= 0
    ):
        errors.append("timeout_ms must be a positive integer when present")
    source = value.get("source")
    if source is not None and not isinstance(source, str):
        errors.append("source must be a string when present")
    if errors:
        return _invalid("invalid_line", "; ".join(errors), value)
    return ReplayEvent(
        line_no=line_no,
        raw_text=text,
        ts=ts.strip(),
        channel=channel.strip(),
        direction=direction,
        frame_hex=frame_hex,
        pair_id=pair_id.strip() if isinstance(pair_id, str) else "",
        timeout_ms=timeout_ms,
        source=source.strip() if isinstance(source, str) else "",
    )


def count_lines(path: str | Path) -> int:
    try:
        with Path(path).open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError as exc:
        raise ReplayError("replay_read", f"cannot read replay file: {exc}") from exc


def load_replay(path: str | Path) -> ReplaySource:
    target = Path(path)
    if not target.is_file():
        raise ReplayError("replay_missing", f"replay file does not exist: {target}")
    try:
        digest = sha256_file(target)
    except OSError as exc:
        raise ReplayError("replay_read", f"cannot hash replay file: {exc}") from exc
    return ReplaySource(path=target, sha256=digest, total_lines=count_lines(target))
