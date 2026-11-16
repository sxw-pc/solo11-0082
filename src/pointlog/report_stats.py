"""Build the stable report summary object from ledger queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import ContractSet
from .quality import CALIBRATION_UNKNOWN, GOOD, OVERFLOW, UNDER_RANGE, is_pending
from .store import Store
from .store_queries import (
    batch_totals,
    device_stats,
    issue_distribution,
    pending_recollect,
    point_stats,
    quality_distribution,
)


class ReportError(RuntimeError):
    pass


def _view_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "readings": len(rows),
        "valid_readings": sum(1 for row in rows if row["quality_code"] == GOOD),
        "out_of_range": sum(
            1 for row in rows if row["quality_code"] in (OVERFLOW, UNDER_RANGE)
        ),
        "calibration_unknown": sum(
            1 for row in rows if row["quality_code"] == CALIBRATION_UNKNOWN
        ),
        "pending_points": len({
            (row["device_id"], row["point_key"])
            for row in rows
            if is_pending(row["quality_code"])
        }),
        "derived_readings": sum(1 for row in rows if row["basis"] == "derived"),
    }


def calibration_views(
    store: Store, batch_id: str, contract: ContractSet
) -> dict[str, Any]:
    """Build the contract-authorized calibration views for calibrated points.

    ``as_recorded`` freezes the conclusions drawn with the calibration known
    at first capture; ``as_effective`` shows the latest derived conclusion of
    the current effective-period mapping. Both read the same raw_value and
    never touch frame CRC evidence or the source-file digest.
    """
    calibrated_keys = {
        point.point_key for point in contract.points if point.calibrations
    }
    readings = [
        reading
        for reading in store.batch_readings(batch_id)
        if reading["point_key"] in calibrated_keys
    ]
    readings.sort(
        key=lambda row: (row["device_id"], row["point_key"], row["ts"], row["line_no"])
    )
    latest_derived = store.latest_derived_map(batch_id)

    recorded_rows: list[dict[str, Any]] = []
    effective_rows: list[dict[str, Any]] = []
    for reading in readings:
        base = {
            "device_id": reading["device_id"],
            "point_key": reading["point_key"],
            "ts": reading["ts"],
            "line_no": reading["line_no"],
            "raw_value": reading["raw_value"],
            "unit": reading["unit"],
            "enum_label": reading["enum_label"],
        }
        recorded_rows.append({
            **base,
            "engineering_value": reading["engineering_value"],
            "quality_code": reading["quality_code"],
            "calibration_version_id": reading["calibration_version_id"],
            "calibration_registered_at": reading["calibration_registered_at"],
            "basis": "recorded",
        })
        key = (reading["device_id"], reading["point_key"], reading["ts"])
        derived = latest_derived.get(key)
        if derived is None:
            effective_rows.append({
                **base,
                "engineering_value": reading["engineering_value"],
                "quality_code": reading["quality_code"],
                "calibration_version_id": reading["calibration_version_id"],
                "calibration_registered_at": reading["calibration_registered_at"],
                "basis": "recorded",
            })
        else:
            effective_rows.append({
                **base,
                "engineering_value": derived["engineering_value"],
                "quality_code": derived["quality_code"],
                "calibration_version_id": derived["new_calibration_version_id"],
                "calibration_registered_at": derived["new_calibration_registered_at"],
                "basis": "derived",
                "derivation": {
                    "source_reading_id": derived["source_reading_id"],
                    "old_calibration_version_id": derived["old_calibration_version_id"],
                    "new_calibration_version_id": derived["new_calibration_version_id"],
                    "input_sha256": derived["input_sha256"],
                    "created_at": derived["created_at"],
                },
            })

    views: dict[str, Any] = {
        "authorized_views": list(contract.report_views),
    }
    if "as_recorded" in contract.report_views:
        views["as_recorded"] = {
            "basis": "calibration registered at first capture; frozen conclusions",
            "totals": _view_totals(recorded_rows),
            "readings": recorded_rows,
        }
    if "as_effective" in contract.report_views:
        views["as_effective"] = {
            "basis": "latest derived conclusion of the current effective-period mapping",
            "totals": _view_totals(effective_rows),
            "readings": effective_rows,
        }
    return views


def collect_summary(
    store: Store, batch_id: str, contract: ContractSet
) -> dict[str, Any]:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise ReportError(f"batch {batch_id} does not exist")
    totals = batch_totals(store, batch_id)
    pending = pending_recollect(store, batch_id)
    quality = quality_distribution(store, batch_id)
    issues = issue_distribution(store, batch_id)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "batch": {
            "id": batch["id"],
            "status": batch["status"],
            "started_at": batch["started_at"],
            "finished_at": batch["finished_at"],
            "total_lines": int(batch["total_lines"]),
            "processed_lines": int(batch["processed_lines"]),
            "replay_file": Path(batch["replay_path"]).name,
            "replay_sha256": batch["replay_sha256"],
            "contract_file": Path(batch["contract_path"]).name,
            "contract_sha256": batch["contract_sha256"],
        },
        "totals": {
            "frames": totals["frames"],
            "readings": totals["readings"],
            "valid_readings": totals["valid_readings"],
            "issues": totals["issues"],
            "pending_points": totals["pending_points"],
        },
        "quality_code_distribution": quality,
        "issue_distribution": issues,
        "devices": device_stats(store, batch_id, contract),
        "points": point_stats(store, batch_id, contract),
        "pending_recollect": pending,
    }
    if (
        contract.has_calibrations()
        or store.derived_readings(batch_id)
        or contract.report_views != ("as_recorded",)
    ):
        summary["calibration_views"] = calibration_views(store, batch_id, contract)
    return summary
