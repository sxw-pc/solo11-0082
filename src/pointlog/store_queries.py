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


def derived_contracts(store: Store) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT * FROM derived_contracts ORDER BY created_at, id"
    ).fetchall()
    return [dict(row) for row in rows]


def derived_point_stats(
    store: Store, batch_id: str, contract_id: str
) -> list[dict[str, Any]]:
    definitions = store.derived_point_definitions(contract_id)
    counts = store.derived_row_counts(batch_id, contract_id)
    result = []
    for definition in definitions:
        key = (definition["device_id"], definition["point_key"])
        bucket = counts.get(key, {"total": 0, "good": 0, "indeterminate": 0})
        result.append({
            "device_id": definition["device_id"],
            "point_key": definition["point_key"],
            "unit": definition["unit"],
            "expression": definition["expression"],
            "expression_version": definition["expression_version"],
            "error_semantics": definition["error_semantics"],
            "threshold_direction": definition["threshold_direction"],
            "threshold": definition["threshold"],
            "total": bucket["total"],
            "good": bucket["good"],
            "indeterminate": bucket["indeterminate"],
        })
    return result


def derived_reading_summary(
    store: Store, batch_id: str, contract_id: str
) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT * FROM derived_readings WHERE batch_id = ? AND contract_id = ? "
        "ORDER BY device_id, point_key, ts, id",
        (batch_id, contract_id),
    ).fetchall()
    return [dict(row) for row in rows]


def derived_indeterminate_summary(
    store: Store, batch_id: str, contract_id: str
) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT device_id, point_key, ts, reason_codes, sources_json, line_no_json "
        "FROM derived_readings WHERE batch_id = ? AND contract_id = ? "
        "AND quality_code = 'derived_indeterminate' "
        "ORDER BY device_id, point_key, ts",
        (batch_id, contract_id),
    ).fetchall()
    return [dict(row) for row in rows]


def derived_threshold_summary(
    store: Store, batch_id: str, contract_id: str
) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        "SELECT device_id, point_key, ts, error_semantics, threshold, threshold_state, "
        "engineering_value, lower_bound, upper_bound, unit, bound_sigma "
        "FROM derived_readings WHERE batch_id = ? AND contract_id = ? "
        "AND threshold_state <> '' ORDER BY point_key, ts",
        (batch_id, contract_id),
    ).fetchall()
    return [dict(row) for row in rows]
