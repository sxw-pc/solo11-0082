"""Read-only aggregate queries used by reports and the local review console."""

from __future__ import annotations

from typing import Any

from .contract import ContractSet
from .quality import QUALITY_ORDER, severity
from .store import Store


def batch_list(store: Store, limit: int = 100) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT * FROM batches ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def issue_distribution(store: Store, batch_id: str) -> dict[str, int]:
    rows = store.conn.execute(
        "SELECT issue_code, COUNT(*) AS n FROM issues WHERE batch_id = ? "
        "GROUP BY issue_code ORDER BY n DESC, issue_code",
        (batch_id,),
    ).fetchall()
    return {row["issue_code"]: int(row["n"]) for row in rows}


def quality_distribution(store: Store, batch_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in store.conn.execute(
        "SELECT quality_code, COUNT(*) AS n FROM frames WHERE batch_id = ? "
        "GROUP BY quality_code",
        (batch_id,),
    ):
        counts[row["quality_code"]] = counts.get(row["quality_code"], 0) + int(row["n"])
    # Contract points with no replay line have no frame; their synthetic
    # line-0 issue still contributes to the quality distribution.
    for row in store.conn.execute(
        "SELECT quality_code, COUNT(*) AS n FROM issues "
        "WHERE batch_id = ? AND line_no = 0 GROUP BY quality_code",
        (batch_id,),
    ):
        counts[row["quality_code"]] = counts.get(row["quality_code"], 0) + int(row["n"])
    ordered: dict[str, int] = {}
    for code in QUALITY_ORDER:
        if counts.get(code):
            ordered[code] = counts[code]
    for code in sorted(counts):
        if code not in ordered:
            ordered[code] = counts[code]
    return ordered


def device_stats(store: Store, batch_id: str, contract: ContractSet) -> list[dict[str, Any]]:
    reading_rows = {
        row["device_id"]: dict(row)
        for row in store.conn.execute(
            "SELECT device_id, COUNT(*) AS reading_count, "
            "SUM(CASE WHEN quality_code='good' THEN 1 ELSE 0 END) AS good_count "
            "FROM readings WHERE batch_id = ? GROUP BY device_id",
            (batch_id,),
        )
    }
    issue_rows = {
        row["device_id"]: dict(row)
        for row in store.conn.execute(
            "SELECT device_id, COUNT(*) AS issue_count, "
            "SUM(pending_collect) AS pending_count FROM issues "
            "WHERE batch_id = ? GROUP BY device_id",
            (batch_id,),
        )
    }
    result = []
    for device in contract.devices:
        readings = reading_rows.get(device.device_id, {})
        issues = issue_rows.get(device.device_id, {})
        result.append({
            "device_id": device.device_id,
            "label": device.label,
            "reading_count": int(readings.get("reading_count") or 0),
            "good_count": int(readings.get("good_count") or 0),
            "issue_count": int(issues.get("issue_count") or 0),
            "pending_count": int(issues.get("pending_count") or 0),
        })
    return result


def point_stats(store: Store, batch_id: str, contract: ContractSet) -> list[dict[str, Any]]:
    reading_rows = {
        (row["device_id"], row["point_key"]): dict(row)
        for row in store.conn.execute(
            "SELECT device_id, point_key, COUNT(*) AS reading_count, "
            "SUM(CASE WHEN quality_code='good' THEN 1 ELSE 0 END) AS good_count "
            "FROM readings WHERE batch_id = ? GROUP BY device_id, point_key",
            (batch_id,),
        )
    }
    issue_rows = {
        (row["device_id"], row["point_key"]): dict(row)
        for row in store.conn.execute(
            "SELECT device_id, point_key, COUNT(*) AS issue_count, "
            "SUM(pending_collect) AS pending_count FROM issues "
            "WHERE batch_id = ? GROUP BY device_id, point_key",
            (batch_id,),
        )
    }
    result = []
    for point in sorted(contract.points, key=lambda item: (item.device_id, item.point_key)):
        key = (point.device_id, point.point_key)
        readings = reading_rows.get(key, {})
        issues = issue_rows.get(key, {})
        result.append({
            "device_id": point.device_id,
            "point_key": point.point_key,
            "unit": point.unit,
            "data_type": point.data_type,
            "reading_count": int(readings.get("reading_count") or 0),
            "good_count": int(readings.get("good_count") or 0),
            "issue_count": int(issues.get("issue_count") or 0),
            "pending": bool(issues.get("pending_count") or 0),
        })
    return result


def pending_recollect(store: Store, batch_id: str) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT device_id, point_key, issue_code, quality_code, severity, detail, "
        "line_no, ts FROM issues WHERE batch_id = ? AND pending_collect = 1 "
        "ORDER BY device_id, point_key, issue_code, line_no, detail",
        (batch_id,),
    ).fetchall()
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["device_id"], row["point_key"], row["issue_code"], row["quality_code"])
        item = grouped.setdefault(key, {
            "device_id": row["device_id"],
            "point_key": row["point_key"],
            "issue_code": row["issue_code"],
            "quality_code": row["quality_code"],
            "severity": row["severity"] or severity(row["quality_code"]),
            "occurrences": 0,
            "line_numbers": [],
            "details": [],
        })
        item["occurrences"] += 1
        if row["line_no"] and row["line_no"] not in item["line_numbers"]:
            item["line_numbers"].append(int(row["line_no"]))
        if row["detail"] not in item["details"]:
            item["details"].append(row["detail"])
    result = []
    for key in sorted(grouped):
        item = grouped[key]
        item["line_numbers"].sort()
        item["detail"] = " | ".join(item.pop("details"))
        result.append(item)
    return result


def pending_point_count(store: Store, batch_id: str) -> int:
    row = store.conn.execute(
        "SELECT COUNT(DISTINCT device_id || char(31) || point_key) AS n "
        "FROM issues WHERE batch_id = ? AND pending_collect = 1",
        (batch_id,),
    ).fetchone()
    return int(row["n"])


def issue_rows(store: Store, batch_id: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT * FROM issues WHERE batch_id = ? ORDER BY line_no, id LIMIT ?",
        (batch_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def problem_frames(store: Store, batch_id: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT * FROM frames WHERE batch_id = ? AND quality_code <> 'good' "
        "ORDER BY line_no LIMIT ?",
        (batch_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def reading_rows(
    store: Store, batch_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT * FROM readings WHERE batch_id = ? ORDER BY line_no, id LIMIT ?",
        (batch_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def batch_totals(store: Store, batch_id: str) -> dict[str, int]:
    frame_count = store.conn.execute(
        "SELECT COUNT(*) FROM frames WHERE batch_id = ?", (batch_id,)
    ).fetchone()[0]
    reading_count = store.conn.execute(
        "SELECT COUNT(*) FROM readings WHERE batch_id = ?", (batch_id,)
    ).fetchone()[0]
    good_count = store.conn.execute(
        "SELECT COUNT(*) FROM readings WHERE batch_id = ? AND quality_code = 'good'",
        (batch_id,),
    ).fetchone()[0]
    issue_count = store.conn.execute(
        "SELECT COUNT(*) FROM issues WHERE batch_id = ?", (batch_id,)
    ).fetchone()[0]
    return {
        "frames": int(frame_count),
        "readings": int(reading_count),
        "valid_readings": int(good_count),
        "issues": int(issue_count),
        "pending_points": pending_point_count(store, batch_id),
    }


def _parse_lines(value: str) -> list[int]:
    return [int(part) for part in value.split(",") if part.strip()]


def interlock_events(
    store: Store, batch_id: str, rule_id: str | None = None
) -> list[dict[str, Any]]:
    query = (
        "SELECT * FROM interlock_events WHERE batch_id = ?"
        + (" AND rule_id = ?" if rule_id else "")
        + " ORDER BY device_id, rule_id, start_ts, end_ts, event_key"
    )
    params: tuple[Any, ...] = (batch_id,) if rule_id is None else (batch_id, rule_id)
    result = []
    for row in store.conn.execute(query, params):
        item = dict(row)
        item.pop("id", None)  # SQLite's row id is not part of the conclusion
        item["line_numbers"] = _parse_lines(row["line_numbers"])
        item["interruption_lines"] = _parse_lines(row["interruption_lines"])
        result.append(item)
    return result


def interlock_intervals(
    store: Store,
    batch_id: str,
    rule_id: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["batch_id = ?"]
    params: list[Any] = [batch_id]
    if rule_id:
        clauses.append("rule_id = ?")
        params.append(rule_id)
    if state:
        clauses.append("state = ?")
        params.append(state)
    query = (
        "SELECT * FROM interlock_intervals WHERE " + " AND ".join(clauses)
        + " ORDER BY device_id, rule_id, start_epoch, end_epoch, state, reason"
    )
    result = []
    for row in store.conn.execute(query, params):
        item = dict(row)
        item.pop("id", None)
        item["line_numbers"] = _parse_lines(row["line_numbers"])
        result.append(item)
    return result


def interlock_summary(
    store: Store, batch_id: str, contract: ContractSet
) -> dict[str, Any]:
    """Structured three-valued interlock report, separate from point stats."""
    events = interlock_events(store, batch_id)
    intervals = interlock_intervals(store, batch_id)
    rules = []
    totals = {
        "proven_interlock_seconds": 0.0,
        "proven_normal_seconds": 0.0,
        "unknown_seconds": 0.0,
        "event_count": 0,
        "unknown_interval_count": 0,
        "proven_simultaneous_instants": 0,
    }
    for rule in contract.interlocks:
        rule_intervals = [item for item in intervals if item["rule_id"] == rule.rule_id]
        rule_events = [item for item in events if item["rule_id"] == rule.rule_id]
        seconds = {"true": 0.0, "false": 0.0, "unknown": 0.0}
        proven_instants = 0
        for item in rule_intervals:
            seconds[item["state"]] += float(item["duration_seconds"])
            if item["state"] == "true" and item["reason"] == "simultaneous_sample":
                proven_instants += 1
        window_start = min(
            (item["start_ts"] for item in rule_intervals), default=""
        )
        window_end = max((item["end_ts"] for item in rule_intervals), default="")
        unknown = [item for item in rule_intervals if item["state"] == "unknown"]
        rules.append({
            "rule_id": rule.rule_id,
            "version": rule.version,
            "device_id": rule.device_id,
            "description": rule.description,
            "sampling": rule.sampling,
            "points": [
                {
                    "point_key": point.point_key,
                    "threshold": point.threshold,
                    "direction": point.direction,
                    "inclusive": point.inclusive,
                }
                for point in rule.points
            ],
            "min_duration_minutes": rule.min_duration_minutes,
            "max_stale_minutes": rule.max_stale_minutes,
            "window_start_ts": window_start,
            "window_end_ts": window_end,
            "totals": {
                "proven_interlock_seconds": round(seconds["true"], 6),
                "proven_normal_seconds": round(seconds["false"], 6),
                "unknown_seconds": round(seconds["unknown"], 6),
                "event_count": len(rule_events),
                "unknown_interval_count": len(unknown),
                "proven_simultaneous_instants": proven_instants,
            },
            "events": rule_events,
            "intervals": rule_intervals,
            "unknown_intervals": unknown,
        })
        totals["proven_interlock_seconds"] += seconds["true"]
        totals["proven_normal_seconds"] += seconds["false"]
        totals["unknown_seconds"] += seconds["unknown"]
        totals["event_count"] += len(rule_events)
        totals["unknown_interval_count"] += len(unknown)
        totals["proven_simultaneous_instants"] += proven_instants
    for key in totals:
        if isinstance(totals[key], float):
            totals[key] = round(totals[key], 6)
    return {"totals": totals, "rules": rules}


def interlock_unknown_pending(
    store: Store, batch_id: str, contract: ContractSet
) -> list[dict[str, Any]]:
    """Unknown interlock spans as recollect hints; never counted as normal."""
    result = []
    intervals = interlock_intervals(store, batch_id, state="unknown")
    for rule in contract.interlocks:
        for item in intervals:
            if item["rule_id"] != rule.rule_id:
                continue
            point_key = "+".join(rule.point_keys)
            result.append({
                "device_id": rule.device_id,
                "point_key": point_key,
                "issue_code": "interlock_unknown",
                "quality_code": "unknown",
                "severity": "warning",
                "occurrences": 1,
                "line_numbers": item["line_numbers"],
                "detail": (
                    f"interlock {rule.rule_id} v{rule.version} {rule.sampling}: "
                    f"{item['start_ts']}..{item['end_ts']} unknown "
                    f"({item['reason']}); cannot classify as normal"
                ),
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "state": "unknown",
                "start_ts": item["start_ts"],
                "end_ts": item["end_ts"],
                "reason": item["reason"],
            })
    return result
