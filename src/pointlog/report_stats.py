"""Build the stable report summary object from ledger queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import ContractSet
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


def _fence_summary(
    store: Store, batch: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Build the version-fence report section and decide stat trust.

    A batch is trusted only when its fence record exists and ended
    ``verified``; legacy batches without any fence record keep their
    historical stats (they are not invalidated by the new fence).
    """
    fence = store.get_fence(batch["id"])
    if fence is None:
        return {
            "route": "none",
            "source_path": batch["replay_path"],
            "sealed_path": "",
            "sha256": batch["replay_sha256"],
            "byte_count": None,
            "total_lines": int(batch["total_lines"]),
            "source_status": "unknown",
            "result": "not_recorded",
            "detail": "batch predates version-fence provenance",
        }, True
    return {
        "route": fence["route"],
        "source_path": fence["source_path"],
        "sealed_path": fence["sealed_path"],
        "sha256": fence["sha256"],
        "byte_count": fence["byte_count"],
        "total_lines": int(fence["total_lines"]),
        "source_status": fence["source_status"],
        "result": fence["result"],
        "detail": fence["detail"],
    }, fence["result"] == "verified"


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
    devices = device_stats(store, batch_id, contract)
    points = point_stats(store, batch_id, contract)
    version_fence, trusted = _fence_summary(store, batch)
    if not trusted:
        # Frames stay queryable for diagnostics, but the valid-reading and
        # pending-recollect statistics must not be consumed as capture
        # results for this SHA: mark them unavailable everywhere.
        for item in devices:
            item["good_count"] = None
            item["pending_count"] = None
        for item in points:
            item["good_count"] = None
            item["pending"] = None
    return {
        "schema_version": 2,
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
        "version_fence": version_fence,
        "totals": {
            "frames": totals["frames"],
            "readings": totals["readings"],
            "valid_readings": totals["valid_readings"] if trusted else None,
            "issues": totals["issues"],
            "pending_points": totals["pending_points"] if trusted else None,
            "stats_status": "available" if trusted else "unavailable",
        },
        "quality_code_distribution": quality,
        "issue_distribution": issues,
        "devices": devices,
        "points": points,
        "pending_recollect": pending if trusted else None,
    }
