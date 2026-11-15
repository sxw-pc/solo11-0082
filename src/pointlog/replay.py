"""JSONL replay-file fingerprinting and line iteration."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
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
