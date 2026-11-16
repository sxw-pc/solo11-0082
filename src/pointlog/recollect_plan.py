"""Offline recollect planning: merge pending points into reviewable requests.

This module only *plans*. It never opens a serial port, never connects to a
device and never claims that a frame was sent or that a reading succeeded.
Given the pending-recollect issues of one completed batch, the current point
contract and a bus-window budget, it produces a deterministic,
JSON-serialisable plan that duty staff can review before spending a real bus
window:

- ``requests``: the Modbus requests to send, with register span, a reviewable
  request frame and the exact list of pending points each request covers.
- ``points``: one logical recollect obligation per distinct pending point,
  with the covering request id or a concrete blocking reason.
- ``usage``: physical requests vs. logical obligations, budget consumption
  and the number of independent failure chances (one per selected request —
  a failed request affects every point it covers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .contract import RECOLLECT_OBJECTIVES, ContractSet, PointContract, RecollectConfig
from .modbus import build_request
from .util import normalize_number

MAX_REQUEST_SPAN = 125  # Modbus read-request register span limit

# Stable machine-readable blocking reasons for uncovered points.
REASON_POINT_NOT_IN_CONTRACT = "point_not_in_contract"
REASON_DEVICE_MISMATCH = "contract_device_mismatch"
REASON_REQUEST_BUDGET = "request_budget_exceeded"
REASON_WINDOW_TIME = "window_time_exceeded"

_EPSILON = 1e-9


@dataclass
class _Obligation:
    """One logical recollect duty: a distinct pending device/point pair."""

    device_id: str
    point_key: str
    issue_codes: list[str] = field(default_factory=list)
    occurrences: int = 0
    line_numbers: list[int] = field(default_factory=list)
    point: PointContract | None = None
    priority: int | None = None
    expires_at: str | None = None
    covered_by: str | None = None
    block: dict[str, str] | None = None


@dataclass
class _Candidate:
    """One physical request that covers a disjoint set of obligations."""

    slave_id: int
    function: int
    start: int
    count: int
    obligations: list[_Obligation]
    incidental: list[str]
    urgency: tuple = ()
    scheduled_start: float | None = None
    scheduled_end: float | None = None
    rejection: dict[str, str] | None = None


def _parse_base_time(text: str) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_ts(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _expiry(base: datetime | None, ttl_seconds: float | None) -> str | None:
    if base is None or ttl_seconds is None:
        return None
    return _format_ts(base + timedelta(seconds=float(ttl_seconds)))


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _registers_covered(first: int, last: int, intervals: list[list[int]]) -> bool:
    """True when every register in [first, last] is inside some interval."""
    cursor = first
    for start, end in intervals:
        if end < cursor:
            continue
        if start > cursor:
            return False
        cursor = end + 1
        if cursor > last:
            return True
    return cursor > last


def _collect_obligations(
    pending_items: list[dict[str, Any]],
    contract: ContractSet,
    base: datetime | None,
) -> list[_Obligation]:
    """Group pending issue rows into one logical obligation per point."""
    grouped: dict[tuple[str, str], _Obligation] = {}
    for item in pending_items:
        device_id = str(item.get("device_id") or "")
        point_key = str(item.get("point_key") or "")
        key = (device_id, point_key)
        obligation = grouped.get(key)
        if obligation is None:
            point = contract.get(point_key) if point_key else None
            obligation = _Obligation(
                device_id=device_id,
                point_key=point_key,
                point=point,
                priority=point.recollect_priority if point else None,
                expires_at=_expiry(
                    base, point.recollect_ttl_seconds if point else None
                ),
            )
            grouped[key] = obligation
        issue_code = str(item.get("issue_code") or "")
        if issue_code and issue_code not in obligation.issue_codes:
            obligation.issue_codes.append(issue_code)
        obligation.occurrences += int(item.get("occurrences") or 0)
        for line_no in item.get("line_numbers") or []:
            line_no = int(line_no)
            if line_no not in obligation.line_numbers:
                obligation.line_numbers.append(line_no)
    obligations = sorted(
        grouped.values(), key=lambda ob: (ob.device_id, ob.point_key)
    )
    for obligation in obligations:
        obligation.issue_codes.sort()
        obligation.line_numbers.sort()
        if obligation.point is None:
            obligation.block = {
                "code": REASON_POINT_NOT_IN_CONTRACT,
                "detail": "pending issue has no matching point in the current contract",
            }
        elif obligation.point.device_id != obligation.device_id:
            obligation.block = {
                "code": REASON_DEVICE_MISMATCH,
                "detail": (
                    f"issue device {obligation.device_id!r} does not match "
                    f"contract device {obligation.point.device_id!r}"
                ),
            }
    return obligations


def _make_candidate(
    slave_id: int,
    function: int,
    start: int,
    end: int,
    obligations: list[_Obligation],
    contract: ContractSet,
) -> _Candidate:
    pending_keys = {ob.point_key for ob in obligations}
    incidental = sorted(
        point.point_key
        for point in contract.points
        if point.slave_id == slave_id
        and point.function == function
        and point.point_key not in pending_keys
        and point.start >= start
        and point.address_end <= end
    )
    return _Candidate(
        slave_id=slave_id,
        function=function,
        start=start,
        count=end - start + 1,
        obligations=list(obligations),
        incidental=incidental,
    )


def _build_candidates(
    obligations: list[_Obligation], contract: ContractSet
) -> list[_Candidate]:
    """Merge obligations into maximal physical requests per slave/function.

    Two ranges merge only when the joint span stays within 125 registers and
    every gap register belongs to a contract point of the same slave/function
    (unmapped registers are forbidden holes: reading them risks an exception
    that would fail every covered point).  Merging never crosses slave_id or
    function.  Runs are disjoint, so each obligation maps to one candidate.
    """
    groups: dict[tuple[int, int], list[_Obligation]] = {}
    for obligation in obligations:
        if obligation.block is not None or obligation.point is None:
            continue
        point = obligation.point
        groups.setdefault((point.slave_id, point.function), []).append(obligation)
    candidates: list[_Candidate] = []
    for slave_id, function in sorted(groups):
        members = sorted(
            groups[(slave_id, function)],
            key=lambda ob: (ob.point.start, ob.point.count, ob.point.point_key),
        )
        intervals = _merge_intervals([
            (point.start, point.address_end)
            for point in contract.points
            if point.slave_id == slave_id and point.function == function
        ])
        run = [members[0]]
        run_start = members[0].point.start
        run_end = members[0].point.address_end
        for obligation in members[1:]:
            point = obligation.point
            new_start = min(run_start, point.start)
            new_end = max(run_end, point.address_end)
            span_ok = new_end - new_start + 1 <= MAX_REQUEST_SPAN
            gap_ok = point.start <= run_end + 1 or _registers_covered(
                run_end + 1, point.start - 1, intervals
            )
            if span_ok and gap_ok:
                run.append(obligation)
                run_start, run_end = new_start, new_end
            else:
                candidates.append(_make_candidate(
                    slave_id, function, run_start, run_end, run, contract
                ))
                run = [obligation]
                run_start, run_end = point.start, point.address_end
        candidates.append(_make_candidate(
            slave_id, function, run_start, run_end, run, contract
        ))
    return candidates


def _point_urgency(obligation: _Obligation) -> tuple:
    priority = obligation.priority if obligation.priority is not None else 0
    expires = obligation.expires_at
    # Highest priority first; then earliest expiry; missing expiry sorts last.
    return (
        -priority,
        expires is None,
        expires or "",
        obligation.device_id,
        obligation.point_key,
    )


def _select(
    candidates: list[_Candidate],
    objective: str,
    max_requests: int,
    cooldown_s: float,
    cost_s: float,
    window_seconds: float | None,
) -> list[_Candidate]:
    """Choose and schedule a feasible subset instead of truncating the list."""
    if objective == "coverage":
        ordered = sorted(
            candidates,
            key=lambda c: (-len(c.obligations), c.slave_id, c.function, c.start),
        )
    else:
        ordered = sorted(
            candidates,
            key=lambda c: (c.urgency, c.slave_id, c.function, c.start),
        )
    accepted: list[_Candidate] = []
    cursor = 0.0
    last_end: dict[int, float] = {}
    for candidate in ordered:
        if len(accepted) >= max_requests:
            candidate.rejection = {
                "code": REASON_REQUEST_BUDGET,
                "detail": "request budget exhausted by higher-ranked requests",
            }
            continue
        start_at = cursor
        previous = last_end.get(candidate.slave_id)
        if previous is not None:
            start_at = max(start_at, previous + cooldown_s)
        end_at = start_at + cost_s
        if window_seconds is not None and end_at > window_seconds + _EPSILON:
            candidate.rejection = {
                "code": REASON_WINDOW_TIME,
                "detail": "device cooldown pushes the request beyond the bus window",
            }
            continue
        candidate.scheduled_start = start_at
        candidate.scheduled_end = end_at
        cursor = end_at
        last_end[candidate.slave_id] = end_at
        accepted.append(candidate)
    return accepted


def build_recollect_plan(
    pending_items: list[dict[str, Any]],
    contract: ContractSet,
    config: RecollectConfig | None = None,
    *,
    base_time: str = "",
    objective: str | None = None,
    max_requests: int | None = None,
) -> dict[str, Any]:
    """Build a deterministic offline recollect plan.

    ``pending_items`` are grouped issue rows (see
    ``store_queries.pending_recollect``); ``base_time`` anchors TTL expiry
    (the batch finish time) so repeated runs over the same batch, contract
    and budget produce byte-identical plans.
    """
    config = config if config is not None else contract.recollect
    if config is None:
        raise ValueError("recollect planning requires a recollect config")
    objective = objective if objective is not None else config.objective
    if objective not in RECOLLECT_OBJECTIVES:
        raise ValueError(
            f"objective must be one of {', '.join(RECOLLECT_OBJECTIVES)}"
        )
    limit = config.max_requests if max_requests is None else int(max_requests)
    if limit < 0:
        raise ValueError("max_requests must be non-negative")

    base = _parse_base_time(base_time)
    obligations = _collect_obligations(pending_items, contract, base)
    candidates = _build_candidates(obligations, contract)
    for candidate in candidates:
        candidate.urgency = min(
            _point_urgency(ob) for ob in candidate.obligations
        )
    accepted = _select(
        candidates,
        objective,
        limit,
        config.device_cooldown_s,
        config.request_cost_s,
        config.window_seconds,
    )

    requests: list[dict[str, Any]] = []
    for index, candidate in enumerate(accepted, start=1):
        request_id = f"r{index}"
        covers = []
        for obligation in sorted(
            candidate.obligations, key=lambda ob: (ob.device_id, ob.point_key)
        ):
            obligation.covered_by = request_id
            covers.append({
                "device_id": obligation.device_id,
                "point_key": obligation.point_key,
                "priority": obligation.priority,
                "expires_at": obligation.expires_at,
            })
        requests.append({
            "id": request_id,
            "slave_id": candidate.slave_id,
            "function": candidate.function,
            "start": candidate.start,
            "count": candidate.count,
            "request_hex": build_request(
                candidate.slave_id, candidate.function, candidate.start, candidate.count
            ).hex(),
            "covers": covers,
            "incidental_points": candidate.incidental,
            "failure_impact_points": len(covers),
            "scheduled_start_s": normalize_number(candidate.scheduled_start),
            "scheduled_end_s": normalize_number(candidate.scheduled_end),
        })

    for candidate in candidates:
        if candidate.rejection is None:
            continue
        for obligation in candidate.obligations:
            obligation.block = dict(candidate.rejection)

    points: list[dict[str, Any]] = []
    covered = 0
    for obligation in obligations:
        status = "covered" if obligation.covered_by else "uncovered"
        if obligation.covered_by:
            covered += 1
        points.append({
            "device_id": obligation.device_id,
            "point_key": obligation.point_key,
            "issue_codes": obligation.issue_codes,
            "occurrences": obligation.occurrences,
            "line_numbers": obligation.line_numbers,
            "priority": obligation.priority,
            "expires_at": obligation.expires_at,
            "status": status,
            "covered_by": obligation.covered_by,
            "block": None if obligation.covered_by else obligation.block,
        })

    window_used = max(
        (candidate.scheduled_end for candidate in accepted), default=0.0
    )
    return {
        "schema_version": 1,
        "objective": objective,
        "base_time": _format_ts(base) if base else None,
        "budget": {
            "max_requests": limit,
            "device_cooldown_s": normalize_number(config.device_cooldown_s),
            "request_cost_s": normalize_number(config.request_cost_s),
            "window_seconds": (
                None
                if config.window_seconds is None
                else normalize_number(config.window_seconds)
            ),
        },
        "usage": {
            "pending_points": len(obligations),
            "covered_points": covered,
            "uncovered_points": len(obligations) - covered,
            "requests_used": len(accepted),
            "requests_budget": limit,
            "independent_failure_chances": len(accepted),
            "window_used_s": normalize_number(window_used),
        },
        "requests": requests,
        "points": points,
    }
