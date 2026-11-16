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
    store: Store, batch_id: str, contract: ContractSet
) -> dict[str, Any]:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise ReportError(f"batch {batch_id} does not exist")
    totals = batch_totals(store, batch_id)
    pending = pending_recollect(store, batch_id)
    quality = quality_distribution(store, batch_id)
    issues = issue_distribution(store, batch_id)
    return {
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


def _mapping_diff(before: dict, after: dict) -> dict[str, Any]:
    keys = sorted(set(before) | set(after))
    removed = {key: before[key] for key in keys if key not in after}
    added = {key: after[key] for key in keys if key not in before}
    changed = {
        key: {"before": before[key], "after": after[key]}
        for key in keys
        if key in before and key in after and before[key] != after[key]
    }
    return {"added": added, "removed": removed, "changed": changed}


def _keyed_list_diff(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    key_fields: tuple[str, ...],
) -> dict[str, Any]:
    def key(row: dict[str, Any]) -> tuple:
        return tuple(row[field] for field in key_fields)

    before_map = {key(row): row for row in before}
    after_map = {key(row): row for row in after}
    keys = sorted(set(before_map) | set(after_map))
    removed = [before_map[k] for k in keys if k not in after_map]
    added = [after_map[k] for k in keys if k not in before_map]
    changed = []
    for k in keys:
        if k in before_map and k in after_map and before_map[k] != after_map[k]:
            changed.append({"key": list(k),
                            "before": before_map[k], "after": after_map[k]})
    return {"added": added, "removed": removed, "changed": changed}


def diff_summaries(
    original: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    """Deterministic diff of two report summaries.

    Volatile batch bookkeeping (batch id, timestamps, file names, replay hash)
    is intentionally excluded; only reproducible business sections remain.
    Output ordering is fully deterministic so repeated runs are byte-stable.
    """
    sections = (
        "totals",
        "quality_code_distribution",
        "issue_distribution",
    )
    result: dict[str, Any] = {
        section: _mapping_diff(original.get(section, {}), candidate.get(section, {}))
        for section in sections
    }
    result["devices"] = _keyed_list_diff(
        original.get("devices", []), candidate.get("devices", []), ("device_id",)
    )
    result["points"] = _keyed_list_diff(
        original.get("points", []), candidate.get("points", []),
        ("device_id", "point_key"),
    )
    result["pending_recollect"] = _keyed_list_diff(
        original.get("pending_recollect", []),
        candidate.get("pending_recollect", []),
        ("device_id", "point_key", "issue_code", "quality_code"),
    )
    return result
