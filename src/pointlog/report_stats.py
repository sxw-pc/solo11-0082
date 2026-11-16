"""Build the stable report summary object from ledger queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import ContractSet
from .derived import DerivedConfig, SEMANTICS_DESCRIPTION, make_derived_contract_id
from .store import Store
from .store_queries import (
    batch_totals,
    derived_contracts,
    derived_indeterminate_summary,
    derived_point_stats,
    derived_reading_summary,
    derived_threshold_summary,
    device_stats,
    issue_distribution,
    pending_recollect,
    point_stats,
    quality_distribution,
)


class ReportError(RuntimeError):
    pass


def _derived_section(
    store: Store, batch_id: str, derived_config: DerivedConfig | None
) -> dict[str, Any] | None:
    if derived_config is None:
        # A ledger may already hold derived rows produced by a previous run;
        # surface them even when this invocation passed no config object.
        contracts = derived_contracts(store)
        if not contracts:
            return None
        contract_id = contracts[-1]["id"]
        semantics = contracts[-1]["error_semantics"]
    else:
        contract_id = make_derived_contract_id(derived_config.fingerprint)
        semantics = derived_config.default_semantics
    if store.get_derived_contract(contract_id) is None:
        return None
    point_rows = derived_point_stats(store, batch_id, contract_id)
    good = sum(item["good"] for item in point_rows)
    indeterminate = sum(item["indeterminate"] for item in point_rows)
    threshold_rows = derived_threshold_summary(store, batch_id, contract_id)
    threshold_counts: dict[str, int] = {}
    for row in threshold_rows:
        threshold_counts[row["threshold_state"]] = \
            threshold_counts.get(row["threshold_state"], 0) + 1
    return {
        "contract_id": contract_id,
        "expression_version": derived_config.expression_version if derived_config
        else store.get_derived_contract(contract_id)["expression_version"],
        "error_semantics": semantics,
        "semantics_statement": SEMANTICS_DESCRIPTION[semantics],
        "independent_has_no_guarantee": semantics == "independent",
        "points": point_rows,
        "good_rows": good,
        "indeterminate_rows": indeterminate,
        "readings": derived_reading_summary(store, batch_id, contract_id),
        "indeterminate": derived_indeterminate_summary(store, batch_id, contract_id),
        "thresholds": threshold_rows,
        "threshold_state_counts": threshold_counts,
    }


def collect_summary(
    store: Store, batch_id: str, contract: ContractSet,
    derived_config: DerivedConfig | None = None,
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
    derived = _derived_section(store, batch_id, derived_config)
    if derived is not None:
        summary["derived"] = derived
    return summary
