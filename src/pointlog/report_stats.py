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


def collect_summary(
    store: Store,
    batch_id: str,
    contract: ContractSet,
    crc_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise ReportError(f"batch {batch_id} does not exist")
    totals = batch_totals(store, batch_id)
    pending = pending_recollect(store, batch_id)
    quality = quality_distribution(store, batch_id)
    issues = issue_distribution(store, batch_id)
    summary = {
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
    if crc_audit is not None:
        # Hypothetical fault-space audit of selected good responses. It is
        # reported alongside the batch facts but never folded into totals,
        # the quality distribution or the pending-recollect list.
        summary["crc_audit"] = crc_audit
    return summary
