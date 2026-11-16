"""Read-only aggregate queries used by reports and the local review console."""

from __future__ import annotations

from typing import Any

from .contract import ContractSet
from .lineproc import SYNTHETIC_LINE_NO, issue_identity
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


def issue_identity_set(store: Store, batch_id: str) -> set[tuple[str, str, str, str, str]]:
    """All issue business identities (device, point, issue, quality, ts).

    The full tuple is used for reproduction matching so that an issue code
    emitted for a different point can never count as a hit.
    """
    rows = store.conn.execute(
        "SELECT device_id, point_key, issue_code, quality_code, ts "
        "FROM issues WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    return {issue_identity(row) for row in rows}


def find_issue_row(
    store: Store,
    batch_id: str,
    *,
    device_id: str,
    point_key: str,
    issue_code: str,
    quality_code: str,
    ts: str,
) -> dict[str, Any] | None:
    row = store.conn.execute(
        "SELECT * FROM issues WHERE batch_id = ? AND device_id = ? AND point_key = ? "
        "AND issue_code = ? AND quality_code = ? AND ts = ? LIMIT 1",
        (batch_id, device_id, point_key, issue_code, quality_code, ts),
    ).fetchone()
    return dict(row) if row is not None else None


def synthetic_missing_issues(store: Store, batch_id: str) -> list[dict[str, Any]]:
    """line-0 ``missing_response`` issues synthesized because a contract point
    never appeared; these are pipeline bookkeeping, not physical frame defects.
    """
    rows = store.conn.execute(
        "SELECT * FROM issues WHERE batch_id = ? AND line_no = ? "
        "ORDER BY device_id, point_key, issue_code, quality_code, ts",
        (batch_id, SYNTHETIC_LINE_NO),
    ).fetchall()
    return [dict(row) for row in rows]


def device_issue_profile(
    store: Store, batch_id: str, device_id: str
) -> dict[str, Any]:
    """Full issue-code / quality-code / pending-point picture for one device.

    Used by device-wide preservation mode: a sub-replay keeps the device
    picture only when the grouped issue identities (including line-0 synthetic
    missing responses) and the pending-recollect point set are identical.
    """
    rows = store.conn.execute(
        "SELECT * FROM issues WHERE batch_id = ? AND device_id = ?",
        (batch_id, device_id),
    ).fetchall()
    identities = sorted(issue_identity(row) for row in rows)
    issue_codes: dict[str, int] = {}
    quality_codes: dict[str, int] = {}
    pending_points: set[tuple[str, str]] = set()
    synthetic: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        issue_codes[row["issue_code"]] = issue_codes.get(row["issue_code"], 0) + 1
        quality_codes[row["quality_code"]] = (
            quality_codes.get(row["quality_code"], 0) + 1
        )
        if row["pending_collect"]:
            pending_points.add((row["device_id"], row["point_key"]))
        if row["line_no"] == SYNTHETIC_LINE_NO:
            synthetic.append(issue_identity(row))
    return {
        "device_id": device_id,
        "issue_code_distribution": dict(sorted(issue_codes.items())),
        "quality_code_distribution": dict(sorted(quality_codes.items())),
        "pending_points": sorted(pending_points),
        "issue_identities": identities,
        "synthetic_identities": sorted(synthetic),
    }
