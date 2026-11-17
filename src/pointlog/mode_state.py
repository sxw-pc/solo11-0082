"""Temporal mode-state verification for enum points.

The module turns a batch's *completed* readings for one enum-mode point into a
time-ordered evidence timeline, then separates confirmed observations from
unknown stretches. Each pair of confirmed observations receives an evidence
grade derived from the contract's declared transition graph and minimum dwell
requirements:

``direct``
    The two observed states are joined by a declared legal edge (or are the
    same state) and the elapsed time respects the exit dwell.
``impossible``
    No legal path in the declared graph fits the elapsed time; the evidence
    positively contradicts the contract.
``unproven``
    A legal path with unobserved intermediate states fits the elapsed time,
    but the evidence does not show that the device actually took it.

The contract authorises one of two verification semantics:

``hop``
    Every hop must be observed. A non-adjacent pair is a path-observation
    violation and should be investigated in the process log.
``bounded``
    Missing intermediate states may be explained when the unknown interval is
    within the declared maximum gap. The pair stays unproven and needs a
    supplemental sample of the intermediate state.

Nothing here writes to the ledger: raw readings and their quality codes are
left exactly as ingested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .codec import CodecError, decode_value
from .contract import ContractSet, ModeStateContract, PointContract
from .quality import (
    EVIDENCE_DIRECT,
    EVIDENCE_IMPOSSIBLE,
    EVIDENCE_UNPROVEN,
    GOOD,
)
from .store import Store

OBSERVATION = "observation"
UNKNOWN = "unknown"

VERDICT_DIRECT = "direct"
VERDICT_IMPOSSIBLE = "impossible"
VERDICT_HOP_NOT_OBSERVED = "hop_not_observed"
VERDICT_MISSED_INTERMEDIATE = "missed_intermediate"
VERDICT_GAP_EXCEEDS_MAX = "gap_exceeds_max_gap"

REASON_LONG_GAP = "long_gap"
REASON_SAME_TIME_CONFLICT = "same_time_conflict"
REASON_UNREADABLE_TS = "unreadable_ts"
REASON_READING_MISSING = "reading_missing"


class ModeReviewError(RuntimeError):
    """Raised when a mode-state review cannot be produced deterministically."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TimelineRow:
    """One piece of evidence for a mode point, ordered by timestamp later."""

    kind: str
    line_no: int
    ts_text: str
    ts: datetime | None
    quality_code: str
    reason: str
    state: int | None = None
    label: str | None = None
    detail: str = ""

    @property
    def sort_key(self) -> tuple:
        if self.ts is None:
            return (datetime.max.replace(tzinfo=timezone.utc), self.line_no, self.kind)
        return (self.ts, self.line_no, self.kind)


def parse_ts(text: str) -> datetime | None:
    """Parse an ISO-8601 timestamp into UTC; return None when unusable."""
    if not isinstance(text, str) or not text.strip():
        return None
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _seconds(delta: timedelta) -> float:
    value = delta.total_seconds()
    return int(value) if value.is_integer() else value


def _state_number(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and not raw.is_integer():
        return None
    return int(raw)


# ---------------------------------------------------------------------------
# Timeline construction
# ---------------------------------------------------------------------------


def build_point_timeline(
    point: PointContract,
    reading_rows: Iterable[dict[str, Any]],
    frame_rows: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic per-point evidence timeline from ledger rows.

    Only ``good`` readings with a parseable timestamp become confirmed
    observations; the raw value must also be a declared enum value. Bad frames,
    exception/failed rows, enum/range anomalies, same-timestamp conflicts and
    gaps longer than the declared maximum become unknown evidence. File order
    is never used to decide ordering: rows are sorted by timestamp and then
    line number, and contradictory same-timestamp observations stay unknown.
    """
    readings = [dict(row) for row in reading_rows]
    frames = [dict(row) for row in frame_rows]
    frame_by_line = {int(row["line_no"]): row for row in frames if row.get("line_no")}
    reading_by_line = {int(row.get("line_no") or 0): row for row in readings}

    rows: list[TimelineRow] = []
    ignored: list[dict[str, Any]] = []

    for row in readings:
        ts_text = str(row.get("ts") or "")
        ts = parse_ts(ts_text)
        line_no = int(row.get("line_no") or 0)
        quality = str(row.get("quality_code") or "")
        state = _state_number(row.get("raw_value"))
        label = row.get("enum_label")
        if ts is None:
            ignored.append({
                "line_no": line_no,
                "ts": ts_text,
                "reason": REASON_UNREADABLE_TS,
                "quality_code": quality,
            })
            continue
        if quality == GOOD and state is not None and state in point.enum:
            rows.append(TimelineRow(
                kind=OBSERVATION,
                line_no=line_no,
                ts_text=ts_text,
                ts=ts,
                quality_code=quality,
                reason="",
                state=state,
                label=str(label) if label is not None else point.enum.get(state),
            ))
        else:
            detail = quality or "unusable_reading"
            if state is not None and state not in point.enum:
                detail = f"{quality}: raw {state} not in enum"
            rows.append(TimelineRow(
                kind=UNKNOWN,
                line_no=line_no,
                ts_text=ts_text,
                ts=ts,
                quality_code=quality,
                reason=quality or "unusable_reading",
                state=state,
                label=str(label) if label is not None else None,
                detail=detail,
            ))

    for frame in frames:
        line_no = int(frame.get("line_no") or 0)
        quality = str(frame.get("quality_code") or "")
        ts_text = str(frame.get("ts") or "")
        ts = parse_ts(ts_text)
        if ts is None:
            continue
        if quality == GOOD:
            if line_no not in reading_by_line:
                rows.append(TimelineRow(
                    kind=UNKNOWN, line_no=line_no, ts_text=ts_text, ts=ts,
                    quality_code=quality, reason=REASON_READING_MISSING,
                    detail="good frame has no stored reading",
                ))
            continue
        if quality == "duplicate":
            if _duplicate_conflicts(ts, frame, frame_by_line, reading_by_line):
                rows.append(TimelineRow(
                    kind=UNKNOWN, line_no=line_no, ts_text=ts_text, ts=ts,
                    quality_code=quality, reason=REASON_SAME_TIME_CONFLICT,
                    state=_decode_frame_state(point, frame),
                    detail="same timestamp carries a different register payload",
                ))
            continue
        rows.append(TimelineRow(
            kind=UNKNOWN,
            line_no=line_no,
            ts_text=ts_text,
            ts=ts,
            quality_code=quality,
            reason=quality or "bad_frame",
            detail=str(frame.get("issue_code") or frame.get("detail") or quality),
        ))

    rows.sort(key=lambda item: item.sort_key)
    # A timestamp that carries two different states cannot support either
    # observation: the file order must not decide which one came first.
    grouped: dict[datetime, set[int]] = {}
    for row in rows:
        if row.ts is None or row.state is None:
            continue
        if row.kind == OBSERVATION or row.reason == REASON_SAME_TIME_CONFLICT:
            grouped.setdefault(row.ts, set()).add(int(row.state))
    conflicting = {ts: states for ts, states in grouped.items() if len(states) > 1}
    if conflicting:
        revised: list[TimelineRow] = []
        for row in rows:
            if (
                row.ts in conflicting
                and row.kind in (OBSERVATION, UNKNOWN)
                and row.state is not None
            ):
                revised.append(TimelineRow(
                    kind=UNKNOWN, line_no=row.line_no, ts_text=row.ts_text,
                    ts=row.ts, quality_code=row.quality_code,
                    reason=REASON_SAME_TIME_CONFLICT, state=row.state,
                    label=row.label,
                    detail=(
                        "same timestamp carries different states "
                        f"{sorted(conflicting[row.ts])}; relative order is undefined"
                    ),
                ))
            else:
                revised.append(row)
        rows = revised
        rows.sort(key=lambda item: item.sort_key)
    observations = _dedupe_observations(rows)
    return {
        "rows": rows,
        "observations": observations,
        "ignored": ignored,
        "out_of_order_input": _is_out_of_order(observations),
    }


def _decode_frame_state(point: PointContract, frame: dict[str, Any]) -> int | None:
    """Best-effort raw enum state from an otherwise rejected duplicate frame."""
    payload = str(frame.get("response_hex") or "")
    if not payload:
        return None
    try:
        raw = bytes.fromhex("".join(payload.split()))
        if len(raw) < 5 or raw[1] & 0x80:
            return None
        expected = point.count * 2
        if len(raw) != 5 + expected or raw[2] != expected:
            return None
        state = _state_number(
            decode_value(raw[3:3 + expected], point.data_type, point.byte_order)
        )
    except (ValueError, CodecError):
        return None
    return state if state is not None and state in point.enum else None


def _duplicate_conflicts(
    ts: datetime,
    frame: dict[str, Any],
    frame_by_line: dict[int, dict[str, Any]],
    reading_by_line: dict[int, dict[str, Any]],
) -> bool:
    """True when a duplicate frame carries a different payload at the same ts."""
    for reading_line, reading in reading_by_line.items():
        if parse_ts(str(reading.get("ts") or "")) != ts:
            continue
        original = frame_by_line.get(reading_line)
        if original is None:
            return True
        if str(original.get("response_hex") or "") != str(frame.get("response_hex") or ""):
            return True
    return False


def _dedupe_observations(rows: list[TimelineRow]) -> list[TimelineRow]:
    """Collapse identical same-time observations; drop conflicting groups."""
    by_ts: dict[datetime, list[TimelineRow]] = {}
    for row in rows:
        if row.kind == OBSERVATION and row.ts is not None:
            by_ts.setdefault(row.ts, []).append(row)
    observations: list[TimelineRow] = []
    for ts in sorted(by_ts):
        group = sorted(by_ts[ts], key=lambda item: (item.line_no, item.state or 0))
        if len({item.state for item in group}) == 1:
            observations.append(group[0])
    return observations


def _is_out_of_order(observations: list[TimelineRow]) -> bool:
    line_order = [row.line_no for row in sorted(observations, key=lambda item: item.line_no)]
    time_order = [row.line_no for row in observations]
    return line_order != time_order


# ---------------------------------------------------------------------------
# Graph search
# ---------------------------------------------------------------------------


def _simple_paths(
    mode: ModeStateContract, source: int, target: int, limit: int = 64
) -> list[tuple[int, ...]]:
    """Enumerate simple legal paths, deterministically and bounded."""
    paths: list[tuple[int, ...]] = []

    def walk(node: int, path: tuple[int, ...], seen: set[int]) -> None:
        if len(paths) >= limit:
            return
        if node == target:
            paths.append(path)
            return
        for nxt in sorted(mode.targets(node)):
            if nxt in seen:
                continue
            walk(node=nxt, path=path + (nxt,), seen=seen | {nxt})

    if source != target:
        walk(source, (source,), {source})
    return paths


def _path_cost(mode: ModeStateContract, path: tuple[int, ...]) -> int:
    """Minimum elapsed seconds for a path.

    Only *intermediate* states are charged. The source state may have been
    entered long before its first observation, so its dwell cannot be used to
    refute an observation; the target is entered at the observed instant and
    its dwell belongs to the next transition.
    """
    return sum(mode.dwell(state) for state in path[1:-1])


def shortest_feasible_path(
    mode: ModeStateContract, source: int, target: int
) -> tuple[tuple[int, ...] | None, int | None]:
    """Return the cheapest legal path and its minimum elapsed seconds."""
    candidates = _simple_paths(mode, source, target)
    if not candidates:
        return None, None
    best = min(candidates, key=lambda path: (_path_cost(mode, path), path))
    return best, _path_cost(mode, best)


def _plausible_windows(
    mode: ModeStateContract, path: tuple[int, ...], elapsed: float
) -> list[dict[str, Any]]:
    """Feasible entry windows for unobserved intermediate states.

    The earliest entry is the sum of intermediate dwells owed before it; the
    latest entry still leaves enough time for the remaining intermediate
    dwells before the target is observed.
    """
    windows: list[dict[str, Any]] = []
    if len(path) <= 2:
        return windows
    for index in range(1, len(path) - 1):
        state = path[index]
        enters = sum(mode.dwell(item) for item in path[1:index])
        remaining = sum(mode.dwell(item) for item in path[index:-1])
        latest = max(enters, int(elapsed) - remaining)
        windows.append({
            "state": state,
            "earliest_offset_seconds": enters,
            "latest_offset_seconds": latest,
            # Symmetric feasible *exit* instants: never earlier than the
            # arrival plus this state's dwell, never later than the target.
            "earliest_exit_offset_seconds": min(int(elapsed), enters + mode.dwell(state)),
            "min_dwell_seconds": mode.dwell(state),
        })
    return windows


def _state_payload(
    mode: ModeStateContract, state: int, enum: dict[int, str]
) -> dict[str, Any]:
    return {
        "raw_value": state,
        "label": enum.get(state),
        "is_reset": mode.reset_state == state,
        "min_dwell_seconds": mode.dwell(state),
        "next_states": list(mode.targets(state)),
    }


# ---------------------------------------------------------------------------
# Pair analysis
# ---------------------------------------------------------------------------


def analyse_pair(
    mode: ModeStateContract,
    enum: dict[int, str],
    from_state: int,
    to_state: int,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """Grade one pair of confirmed observations against the contract."""
    elapsed = float(elapsed_seconds)
    result: dict[str, Any] = {
        "from_state": _state_payload(mode, from_state, enum),
        "to_state": _state_payload(mode, to_state, enum),
        "elapsed_seconds": elapsed,
        "grade": None,
        "verdict": None,
        "assumed_path": None,
        "min_required_seconds": None,
        "plausible_windows": [],
        "via_reset": False,
        "detail": "",
        "recommendation": "",
    }

    if from_state == to_state:
        result.update({
            "grade": EVIDENCE_DIRECT,
            "verdict": VERDICT_DIRECT,
            "assumed_path": [from_state],
            "min_required_seconds": 0,
        })
        return result

    if mode.has_edge(from_state, to_state):
        # The departure instant is 0 or more seconds after the departing state
        # was entered, so a direct edge can never be refuted by its dwell.
        result.update({
            "grade": EVIDENCE_DIRECT,
            "verdict": VERDICT_DIRECT,
            "assumed_path": [from_state, to_state],
            "min_required_seconds": 0,
            "via_reset": to_state == mode.reset_state,
        })
        return result

    path, required = shortest_feasible_path(mode, from_state, to_state)
    if path is None:
        result.update({
            "grade": EVIDENCE_IMPOSSIBLE,
            "verdict": VERDICT_IMPOSSIBLE,
            "detail": (
                f"no legal path from {from_state} to {to_state} exists in the "
                "declared transition graph"
            ),
            "recommendation": "核对状态图声明与观测值；该跳转不可能合法发生",
        })
        return result

    result["assumed_path"] = list(path)
    result["min_required_seconds"] = required
    # A reset is only evidenced by an explicit edge into the reset state. A
    # path that merely passes through it is not an observed reset.
    result["via_reset"] = _ends_on_reset_edge(mode, path)

    if required > elapsed:
        result.update({
            "grade": EVIDENCE_IMPOSSIBLE,
            "verdict": VERDICT_IMPOSSIBLE,
            "detail": (
                f"shortest legal path {_path_text(path)} needs at least {required}s "
                "because of minimum dwell requirements, but only "
                f"{elapsed:g}s elapsed"
            ),
            "recommendation": "核对流程日志；中间状态的最短停留时间无法满足",
        })
        return result

    result["plausible_windows"] = _plausible_windows(mode, path, elapsed)
    if mode.verification == "hop":
        result.update({
            "grade": EVIDENCE_UNPROVEN,
            "verdict": VERDICT_HOP_NOT_OBSERVED,
            "detail": (
                "hop semantics require observing every transition; "
                f"{from_state}->{to_state} is not a declared edge (legal path "
                f"{_path_text(path)} would need unobserved intermediate states)"
            ),
            "recommendation": (
                "检查流程日志并对中间状态补充逐跳观测；当前证据不能证明合法运行"
            ),
        })
        return result

    if elapsed > mode.max_gap_seconds:
        result.update({
            "grade": EVIDENCE_UNPROVEN,
            "verdict": VERDICT_GAP_EXCEEDS_MAX,
            "detail": (
                f"legal path {_path_text(path)} fits {elapsed:g}s, but the unknown "
                "interval exceeds the declared maximum "
                f"{mode.max_gap_seconds}s"
            ),
            "recommendation": "补采中间状态证据；未知间隔超过契约容忍上限",
        })
        return result

    result.update({
        "grade": EVIDENCE_UNPROVEN,
        "verdict": VERDICT_MISSED_INTERMEDIATE,
        "detail": (
            f"legal path {_path_text(path)} fits {elapsed:g}s, but intermediate "
            "states were not sampled"
        ),
        "recommendation": "补充采集中间状态证据后再判定合法运行；当前只能确认未被证伪",
    })
    return result


def _path_text(path: tuple[int, ...]) -> str:
    return "->".join(str(state) for state in path)


def _ends_on_reset_edge(mode: ModeStateContract, path: tuple[int, ...]) -> bool:
    """True only when the observed *final hop* is a declared edge into reset.

    The pair endpoints must themselves be adjacent declared transitions: a
    path that merely passes through or ends at the reset state via unobserved
    intermediate states does not let the report call the jump a reset.
    """
    if mode.reset_state is None or len(path) < 2:
        return False
    return (
        path[-1] == mode.reset_state
        and path[-2] == path[0]
        and mode.has_edge(path[-2], path[-1])
    )


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


@dataclass
class _Region:
    start: datetime
    end: datetime
    start_text: str
    end_text: str
    reasons: set[str] = field(default_factory=set)
    line_numbers: set[int] = field(default_factory=set)
    details: set[str] = field(default_factory=set)

    def merge(self, other: _Region) -> None:
        if other.end > self.end:
            self.end = other.end
            self.end_text = other.end_text
        self.reasons |= other.reasons
        self.line_numbers |= other.line_numbers
        self.details |= other.details


def _merge_regions(regions: list[_Region]) -> list[_Region]:
    ordered = sorted(regions, key=lambda item: (item.start, item.end, sorted(item.reasons)))
    merged: list[_Region] = []
    for region in ordered:
        if merged and region.start <= merged[-1].end:
            merged[-1].merge(region)
        else:
            merged.append(_Region(
                start=region.start, end=region.end,
                start_text=region.start_text, end_text=region.end_text,
                reasons=set(region.reasons),
                line_numbers=set(region.line_numbers),
                details=set(region.details),
            ))
    return merged


def _confirmed_segment(
    mode: ModeStateContract, enum: dict[int, str], rows: list[TimelineRow]
) -> dict[str, Any]:
    first, last = rows[0], rows[-1]
    state = int(first.state)  # type: ignore[arg-type]
    return {
        "kind": "confirmed",
        "start_ts": first.ts_text,
        "end_ts": last.ts_text,
        "duration_seconds": _seconds(last.ts - first.ts),  # type: ignore[operator]
        "state": _state_payload(mode, state, enum),
        "start_line": first.line_no,
        "end_line": last.line_no,
        "line_numbers": sorted({row.line_no for row in rows}),
        "observation_count": len(rows),
        "evidence_grade": EVIDENCE_DIRECT,
        "detail": "confirmed observations of one state",
    }


def build_segments(
    mode: ModeStateContract, enum: dict[int, str], timeline: dict[str, Any]
) -> list[dict[str, Any]]:
    """Split the timeline into confirmed and unknown segments.

    Unknown evidence that falls between two confirmed observations becomes a
    single unknown segment spanning that interval, however many bad frames or
    distinct reasons it contains. Overlapping unknown evidence elsewhere is
    merged the same way, so one unobserved stretch is never reported as several
    independent faults.
    """
    observations: list[TimelineRow] = timeline["observations"]
    max_gap = mode.max_gap_seconds
    unknown_rows = [
        row for row in timeline["rows"]
        if row.kind == UNKNOWN and row.ts is not None
    ]

    # Unknown evidence between two confirmed observations. A single bad row at
    # an endpoint timestamp still taints that observation, so the region spans
    # the whole closed interval between the neighbouring observations.
    between: list[_Region] = []
    covered_rows: set[int] = set()
    for previous, current in zip(observations, observations[1:]):
        start, end = previous.ts, current.ts  # type: ignore[assignment]
        inside = [
            row for row in unknown_rows
            if start <= row.ts <= end  # type: ignore[operator]
        ]
        gap = _seconds(end - start)  # type: ignore[operator]
        if not inside and gap <= max_gap:
            continue
        region = _Region(
            start=start, end=end,  # type: ignore[arg-type]
            start_text=previous.ts_text, end_text=current.ts_text,
            line_numbers={previous.line_no, current.line_no},
        )
        for row in inside:
            covered_rows.add(id(row))
            region.reasons.add(row.reason)
            region.line_numbers.add(row.line_no)
            if row.detail:
                region.details.add(row.detail)
        if gap > max_gap:
            region.reasons.add(REASON_LONG_GAP)
            region.details.add(
                f"gap {gap:g}s exceeds max_gap_seconds {max_gap}s"
            )
        between.append(region)

    # Leftover unknown evidence before the first or after the last confirmed
    # observation, or when there is no pair at all. Rows already folded into a
    # between-pair region are not repeated here.
    leftovers: list[_Region] = []
    for row in unknown_rows:
        if id(row) in covered_rows:
            continue
        leftovers.append(_Region(
            start=row.ts, end=row.ts, start_text=row.ts_text, end_text=row.ts_text,
            reasons={row.reason}, line_numbers={row.line_no},
            details={row.detail} if row.detail else set(),
        ))
    unknown_regions = _merge_regions(between + leftovers)
    # Confirmed runs break wherever an unknown region intersects the interval
    # between consecutive confirmed observations (including a bad row that
    # shares an endpoint timestamp). A state change between two adjacent
    # observations is a *pair* fact, reported by the evidence table, not an
    # unknown stretch.
    # Confirmed runs group contiguous observations of one state and break
    # wherever an unknown region intersects the interval between consecutive
    # observations (including a bad row sharing an endpoint timestamp).
    segments: list[dict[str, Any]] = []
    current: list[TimelineRow] = []
    for row in observations:
        if not current:
            current = [row]
            continue
        previous = current[-1]
        if (
            row.state != previous.state
            or _region_blocks(previous.ts, row.ts, unknown_regions)  # type: ignore[arg-type]
        ):
            segments.append(_confirmed_segment(mode, enum, current))
            current = [row]
        else:
            current.append(row)
    if current:
        segments.append(_confirmed_segment(mode, enum, current))

    for region in unknown_regions:
        segments.append({
            "kind": "unknown",
            "start_ts": region.start_text,
            "end_ts": region.end_text,
            "duration_seconds": _seconds(region.end - region.start),
            "state": None,
            "start_line": min(region.line_numbers) if region.line_numbers else 0,
            "end_line": max(region.line_numbers) if region.line_numbers else 0,
            "line_numbers": sorted(region.line_numbers),
            "observation_count": 0,
            "reasons": sorted(region.reasons),
            "evidence_grade": "unknown",
            "detail": "; ".join(sorted(region.details)) if region.details else
                      ", ".join(sorted(region.reasons)),
        })

    segments.sort(key=lambda item: (
        parse_ts(str(item["start_ts"])) or datetime.max.replace(tzinfo=timezone.utc),
        0 if item["kind"] == "confirmed" else 1,
        str(item.get("state")),
    ))
    return segments


def _segment_blocks(
    start: datetime, end: datetime, segments: list[dict[str, Any]]
) -> bool:
    for segment in segments:
        if segment["kind"] != "unknown":
            continue
        seg_start = parse_ts(str(segment["start_ts"]))
        seg_end = parse_ts(str(segment["end_ts"]))
        if seg_start is None or seg_end is None:  # pragma: no cover - built above
            continue
        if seg_start <= end and seg_end >= start:
            return True
    return False


def _region_blocks(
    start: datetime, end: datetime, regions: list[_Region]
) -> bool:
    """True when an unknown region intersects the closed interval [start, end]."""
    if end <= start:  # pragma: no cover - observations are strictly ordered
        return False
    for region in regions:
        if region.start <= end and region.end >= start:
            return True
    return False


# ---------------------------------------------------------------------------
# Point and batch review
# ---------------------------------------------------------------------------


def analyse_timeline(point: PointContract, timeline: dict[str, Any]) -> dict[str, Any]:
    """Grade a pre-built timeline against the point's mode contract."""
    mode = point.mode_state
    if mode is None:  # pragma: no cover - guarded by caller
        raise ModeReviewError("not_mode_point", f"{point.point_key} has no mode_state")
    segments = build_segments(mode, point.enum, timeline)
    # Every adjacent pair of confirmed observations is graded, even when an
    # unknown stretch separates them: the unknown segment explains *why* the
    # hop was unobserved, but the pair still needs an evidence conclusion.
    observations: list[TimelineRow] = timeline["observations"]
    transitions: list[dict[str, Any]] = []
    for previous, current in zip(observations, observations[1:]):
        pair = analyse_pair(
            mode,
            point.enum,
            int(previous.state),  # type: ignore[arg-type]
            int(current.state),  # type: ignore[arg-type]
            _seconds(current.ts - previous.ts),  # type: ignore[operator]
        )
        pair.update({
            "from_ts": previous.ts_text,
            "to_ts": current.ts_text,
            "from_line": previous.line_no,
            "to_line": current.line_no,
            "from_label": previous.label,
            "to_label": current.label,
            "separated_by_unknown": _segment_blocks(
                previous.ts, current.ts, segments  # type: ignore[arg-type]
            ),
        })
        for window in pair["plausible_windows"]:
            window["earliest_ts"] = _iso(
                previous.ts + timedelta(seconds=window["earliest_offset_seconds"])  # type: ignore[operator]
            )
            window["latest_ts"] = _iso(
                previous.ts + timedelta(seconds=window["latest_offset_seconds"])  # type: ignore[operator]
            )
            window["label"] = point.enum.get(window["state"])
        transitions.append(pair)

    summary = {
        "direct": sum(1 for item in transitions if item["grade"] == EVIDENCE_DIRECT),
        "unproven": sum(1 for item in transitions if item["grade"] == EVIDENCE_UNPROVEN),
        "impossible": sum(
            1 for item in transitions if item["grade"] == EVIDENCE_IMPOSSIBLE
        ),
        "transitions": len(transitions),
        "unknown_segments": sum(1 for item in segments if item["kind"] == "unknown"),
        "confirmed_segments": sum(
            1 for item in segments if item["kind"] == "confirmed"
        ),
        "observations": len(timeline["observations"]),
        "ignored_rows": len(timeline["ignored"]),
    }
    return {
        "device_id": point.device_id,
        "point_key": point.point_key,
        "verification": mode.verification,
        "max_gap_seconds": mode.max_gap_seconds,
        "reset_state": _state_payload(mode, mode.reset_state, point.enum)
        if mode.reset_state is not None else None,
        "states": [
            {"raw_value": state, "label": point.enum.get(state)}
            for state in mode.states
        ],
        "transitions_declared": [
            {"from": source, "to": target}
            for source in sorted(mode.transitions)
            for target in mode.transitions[source]
        ],
        "min_dwell_seconds": {
            str(state): mode.dwell(state)
            for state in sorted(mode.min_dwell_seconds)
        },
        "out_of_order_input": timeline["out_of_order_input"],
        "ignored": list(timeline["ignored"]),
        "segments": segments,
        "transitions": transitions,
        "summary": summary,
    }


def review_point(store: Store, batch_id: str, point: PointContract) -> dict[str, Any]:
    """Review one mode point using only its stored readings and frames."""
    if point.mode_state is None:  # pragma: no cover - guarded by caller
        raise ModeReviewError("not_mode_point", f"{point.point_key} has no mode_state")
    timeline = build_point_timeline(
        point,
        _point_rows(store, "readings", batch_id, point),
        _point_rows(store, "frames", batch_id, point),
    )
    return analyse_timeline(point, timeline)


def mode_state_review(
    store: Store, batch_id: str, contract: ContractSet
) -> list[dict[str, Any]]:
    """Review every enum-mode point in a completed batch, stably ordered."""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise ModeReviewError("batch_missing", f"batch {batch_id} does not exist")
    if batch["status"] != "completed":
        raise ModeReviewError(
            "batch_not_completed",
            f"batch {batch_id} is {batch['status']}; mode-state review requires "
            "a completed batch",
        )
    points = sorted(
        (point for point in contract.points if point.mode_state is not None),
        key=lambda item: (item.device_id, item.point_key),
    )
    return [review_point(store, batch_id, point) for point in points]


def _point_rows(
    store: Store, table: str, batch_id: str, point: PointContract
) -> list[dict[str, Any]]:
    if table == "readings":
        query = (
            "SELECT line_no, ts, source, raw_value, quality_code, enum_label "
            "FROM readings WHERE batch_id = ? AND device_id = ? AND point_key = ? "
            "ORDER BY line_no, id"
        )
    else:
        query = (
            "SELECT line_no, ts, quality_code, issue_code, detail, response_hex "
            "FROM frames WHERE batch_id = ? AND device_id = ? AND point_key = ? "
            "ORDER BY line_no"
        )
    rows = store.conn.execute(
        query, (batch_id, point.device_id, point.point_key)
    ).fetchall()
    return [dict(row) for row in rows]
