"""Shortest reproducible sub-replay search for local quality-issue diagnosis.

Given a *completed* batch and one or more target issue business identities
(device/point/issue_code/quality_code/timestamp), this module enumerates
subsets of the original replay's *physical* JSONL lines (complete lines,
relative order preserved, bytes/timestamps/source untouched) and replays each
candidate through the untouched :func:`pointlog.pipeline.run_capture` pipeline
in an isolated SQLite database.

Two preservation modes are supported:

``target``
    Keep only what is needed for the requested issue identities to reappear.

``device``
    Additionally preserve the target device's full issue-code / quality-code /
    pending-recollect-point distribution from the original batch.

For inputs of at most :data:`MAX_EXACT_INPUT_LINES` lines the search is exact:
subsets are examined in increasing cardinality with original line-number
sequences used as the deterministic tie-breaker, so the first feasible
candidate is the unique shortest, lexicographically smallest answer. Beyond
the limit the search refuses rather than returning an uncertified greedy
result.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from .contract import load_contract
from .lineproc import SYNTHETIC_LINE_NO
from .pipeline import run_isolated_capture
from .report_stats import collect_summary, diff_summaries
from .store import Store
from .store_queries import (
    device_issue_profile,
    find_issue_row,
    issue_identity_set,
    synthetic_missing_issues,
)
from .util import (
    atomic_write_bytes,
    atomic_write_text,
    sha256_file,
    sha256_text,
)

#: Exact exhaustive search is only guaranteed up to this many input lines.
MAX_EXACT_INPUT_LINES = 12

#: Preservation mode constants.
TARGET_MODE = "target"
DEVICE_MODE = "device"
PRESERVATION_MODES = (TARGET_MODE, DEVICE_MODE)

PROOF_SCHEMA_VERSION = 1


class MinReplayError(RuntimeError):
    """Base error for shortest-sub-replay diagnostics."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class MinReplayLimitError(MinReplayError):
    """Raised when the input exceeds the exact-search line limit."""


class MinReplayInfeasibleError(MinReplayError):
    """Raised when no subset of the replay reproduces the requested target."""


@dataclass(frozen=True)
class TargetSpec:
    """Business identity of one issue to reproduce (all five fields matter)."""

    device_id: str
    point_key: str
    issue_code: str
    quality_code: str
    ts: str

    def as_tuple(self) -> tuple[str, str, str, str, str]:
        return (
            self.device_id,
            self.point_key,
            self.issue_code,
            self.quality_code,
            self.ts,
        )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "TargetSpec":
        return cls(
            device_id=str(row["device_id"]),
            point_key=str(row["point_key"]),
            issue_code=str(row["issue_code"]),
            quality_code=str(row["quality_code"]),
            ts=str(row["ts"]),
        )


@dataclass
class MinReplayResult:
    status: str
    mode: str
    guarantee: str
    original_replay_path: str
    original_replay_sha256: str
    sub_replay_sha256: str
    contract_sha256: str
    original_batch_id: str
    sub_batch_id: str
    total_input_lines: int
    sub_line_count: int
    targets: list[dict[str, Any]]
    target_present: list[dict[str, Any]]
    line_mapping: list[dict[str, int]]
    report_diff: dict[str, Any]
    synthetic_missing: dict[str, list[dict[str, Any]]]
    minimality: dict[str, Any]
    sub_replay_bytes: bytes = field(repr=False, default=b"")


def _read_physical_lines(replay_path: Path) -> list[bytes]:
    """Return the exact physical bytes of every JSONL line, newline included."""
    with replay_path.open("rb") as handle:
        return handle.readlines()


def _validate_targets(
    store: Store, batch_id: str, targets: tuple[TargetSpec, ...]
) -> list[dict[str, Any]]:
    if not targets:
        raise MinReplayError("empty_target", "at least one target issue is required")
    seen: set[tuple[str, str, str, str, str]] = set()
    validated: list[dict[str, Any]] = []
    for target in targets:
        identity = target.as_tuple()
        if identity in seen:
            raise MinReplayError(
                "duplicate_target", f"target identity listed twice: {identity}"
            )
        seen.add(identity)
        row = find_issue_row(
            store, batch_id,
            device_id=target.device_id,
            point_key=target.point_key,
            issue_code=target.issue_code,
            quality_code=target.quality_code,
            ts=target.ts,
        )
        if row is None:
            raise MinReplayError(
                "target_not_in_batch",
                f"batch {batch_id} has no issue with identity {identity}",
            )
        validated.append({
            "device_id": row["device_id"],
            "point_key": row["point_key"],
            "issue_code": row["issue_code"],
            "quality_code": row["quality_code"],
            "ts": row["ts"],
            "line_no": int(row["line_no"]),
            "synthetic": bool(row["line_no"] == SYNTHETIC_LINE_NO),
        })
    return validated


def _lines_touching_points(
    store: Store, batch_id: str, point_keys: set[str]
) -> set[int]:
    if not point_keys:
        return set()
    rows = store.conn.execute(
        "SELECT DISTINCT line_no FROM frames WHERE batch_id = ? "
        "AND contract_matched = 1 AND point_key IN (%s)"
        % ",".join("?" for _ in point_keys),
        (batch_id, *sorted(point_keys)),
    ).fetchall()
    return {int(row["line_no"]) for row in rows if int(row["line_no"]) > 0}


def _pruning_sets(
    store: Store,
    batch_id: str,
    target_rows: list[dict[str, Any]],
    mode: str,
) -> tuple[set[int], set[int]]:
    """Lines any feasible subset must include / must exclude (sound pruning).

    Frame-originated issues can only reappear if their physical line is kept;
    synthetic line-0 missing responses can only stay synthetic while every
    line matching that contract point is excluded.
    """
    required: set[int] = set()
    excluded: set[int] = set()
    frame_targets = [row for row in target_rows if not row["synthetic"]]
    synthetic_points = {
        row["point_key"] for row in target_rows if row["synthetic"]
    }
    for row in frame_targets:
        required.add(int(row["line_no"]))
    if mode == DEVICE_MODE:
        device_id = frame_targets[0]["device_id"] if frame_targets else (
            target_rows[0]["device_id"]
        )
        # Every frame-originated issue of the device must remain reproducible.
        device_rows = store.conn.execute(
            "SELECT line_no FROM issues WHERE batch_id = ? AND device_id = ? "
            "AND line_no <> ?",
            (batch_id, device_id, SYNTHETIC_LINE_NO),
        ).fetchall()
        required.update(int(row["line_no"]) for row in device_rows)
        # Every originally-synthetic missing point of the device must stay
        # unseen, otherwise its device distribution changes.
        device_synth = store.conn.execute(
            "SELECT DISTINCT point_key FROM issues WHERE batch_id = ? "
            "AND device_id = ? AND line_no = ?",
            (batch_id, device_id, SYNTHETIC_LINE_NO),
        ).fetchall()
        synthetic_points.update(row["point_key"] for row in device_synth)
    excluded |= _lines_touching_points(store, batch_id, synthetic_points)
    return required, excluded


def _synthetic_payload(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "device_id": row["device_id"],
        "point_key": row["point_key"],
        "issue_code": row["issue_code"],
        "quality_code": row["quality_code"],
        "ts": row["ts"],
        "pending_collect": int(row["pending_collect"]),
        "detail": row["detail"],
    } for row in rows]


def _evaluate(
    candidate: tuple[int, ...],
    *,
    physical_lines: list[bytes],
    contract_path: Path,
    work_root: Path,
) -> tuple[Store, Path, str, str]:
    """Run the full original pipeline on one candidate subset.

    Returns the read-only store, its run directory (for cleanup), the payload
    sha256 and the isolated batch id.
    """
    run_dir = Path(tempfile.mkdtemp(prefix="cand-", dir=str(work_root)))
    sub_path = run_dir / "sub_replay.jsonl"
    payload = b"".join(physical_lines[index - 1] for index in candidate)
    sub_path.write_bytes(payload)
    run_result, db_path = run_isolated_capture(
        replay_path=sub_path,
        contract_path=contract_path,
        work_dir=run_dir,
    )
    sub_store = Store(db_path, read_only=True)
    return sub_store, run_dir, sha256_text(payload.decode("utf-8")), run_result.batch_id


def find_min_replay(
    *,
    batch_db_path: str | Path,
    batch_id: str,
    targets: tuple[TargetSpec, ...] | list[TargetSpec],
    mode: str = TARGET_MODE,
    contract_path: str | Path | None = None,
    replay_path: str | Path | None = None,
    max_input_lines: int = MAX_EXACT_INPUT_LINES,
) -> MinReplayResult:
    """Find the shortest reproducible sub-replay for the given targets."""
    if mode not in PRESERVATION_MODES:
        raise MinReplayError(
            "invalid_mode", f"mode must be one of {PRESERVATION_MODES}, got {mode!r}"
        )
    targets = tuple(targets)
    source_store = Store(batch_db_path, read_only=True)
    try:
        batch = source_store.get_batch(batch_id)
        if batch is None:
            raise MinReplayError(
                "batch_not_found", f"batch {batch_id} does not exist in {batch_db_path}"
            )
        if batch["status"] != "completed":
            raise MinReplayError(
                "batch_not_completed",
                f"batch {batch_id} is {batch['status']}; only completed batches qualify",
            )
        target_rows = _validate_targets(source_store, batch_id, targets)
        if mode == DEVICE_MODE:
            devices = {row["device_id"] for row in target_rows}
            if len(devices) != 1:
                raise MinReplayError(
                    "device_mode_requires_single_device",
                    "device-wide preservation needs all targets on one device, "
                    f"got {sorted(devices)}",
                )
        target_identities = {target.as_tuple() for target in targets}
        replay_file = Path(replay_path) if replay_path is not None else Path(
            batch["replay_path"]
        )
        contract_file = Path(contract_path) if contract_path is not None else Path(
            batch["contract_path"]
        )
        if not replay_file.is_file():
            raise MinReplayError(
                "replay_missing", f"original replay is not available: {replay_file}"
            )
        if not contract_file.is_file():
            raise MinReplayError(
                "contract_missing", f"contract is not available: {contract_file}"
            )
        actual_replay_sha = sha256_file(replay_file)
        if actual_replay_sha != batch["replay_sha256"]:
            raise MinReplayError(
                "replay_sha_mismatch",
                f"{replay_file} sha256 {actual_replay_sha} != batch fingerprint "
                f"{batch['replay_sha256']}",
            )
        contract_sha = sha256_file(contract_file)
        if contract_sha != batch["contract_sha256"]:
            raise MinReplayError(
                "contract_sha_mismatch",
                f"{contract_file} sha256 {contract_sha} != batch fingerprint "
                f"{batch['contract_sha256']}",
            )

        physical_lines = _read_physical_lines(replay_file)
        n_lines = len(physical_lines)
        if n_lines != int(batch["total_lines"]):
            raise MinReplayError(
                "replay_line_mismatch",
                f"replay has {n_lines} physical lines but batch recorded "
                f"{batch['total_lines']}",
            )
        if n_lines > max_input_lines:
            raise MinReplayLimitError(
                "input_too_large",
                f"replay has {n_lines} lines, exact shortest-subset search is only "
                f"guaranteed up to {max_input_lines}; refusing to return an "
                "unverified greedy result",
            )

        required, excluded = _pruning_sets(source_store, batch_id, target_rows, mode)
        if required & excluded:
            raise MinReplayInfeasibleError(
                "target_not_reproducible",
                "a required frame line belongs to a point that must remain unseen; "
                f"conflicting original lines: {sorted(required & excluded)}",
            )
        optional = [
            index
            for index in range(1, n_lines + 1)
            if index not in required and index not in excluded
        ]
        if mode == DEVICE_MODE:
            device_id = target_rows[0]["device_id"]
            original_profile = device_issue_profile(source_store, batch_id, device_id)
        original_summary = collect_summary(
            source_store, batch_id, load_contract(contract_file)
        )
        # Identity tuples of every line-0 issue the original batch synthesized.
        original_synthetic_ids = {
            (row["device_id"], row["point_key"], row["issue_code"],
             row["quality_code"], row["ts"])
            for row in synthetic_missing_issues(source_store, batch_id)
        }
    finally:
        source_store.close()

    work_root = Path(tempfile.mkdtemp(prefix="min-replay-"))
    subsets_enumerated = 0
    subsets_replayed = 0
    subsets_pruned = 0
    winner: tuple[int, ...] | None = None
    winner_store: Store | None = None
    winner_run_dir: Path | None = None
    winner_payload_sha = ""
    winner_batch_id = ""
    try:
        for extra_count in range(len(optional) + 1):
            if winner is not None:
                break
            for extra in combinations(optional, extra_count):
                candidate = tuple(sorted(required | set(extra)))
                subsets_enumerated += 1
                # Sound structural pruning: frame targets need their physical
                # lines; synthetic targets need all point lines excluded.
                if not required <= set(candidate) or excluded & set(candidate):
                    subsets_pruned += 1
                    continue
                sub_store, run_dir, payload_sha, sub_batch_id = _evaluate(
                    candidate,
                    physical_lines=physical_lines,
                    contract_path=contract_file,
                    work_root=work_root,
                )
                subsets_replayed += 1
                try:
                    reproduced = issue_identity_set(sub_store, sub_batch_id)
                    if not target_identities <= reproduced:
                        continue
                    if mode == TARGET_MODE:
                        feasible = True
                    else:
                        feasible = (
                            device_issue_profile(
                                sub_store, sub_batch_id, device_id
                            )
                            == original_profile
                        )
                    if feasible:
                        winner = candidate
                        winner_store = sub_store
                        winner_run_dir = run_dir
                        winner_payload_sha = payload_sha
                        winner_batch_id = sub_batch_id
                        break
                finally:
                    if winner_store is not sub_store:
                        sub_store.close()
                        shutil.rmtree(run_dir, ignore_errors=True)
        if winner is None:
            raise MinReplayInfeasibleError(
                "target_not_reproducible",
                "exhausted every admissible subset of the replay without "
                "reproducing the requested target identity set",
            )

        sub_batch_id = winner_batch_id
        sub_summary = collect_summary(
            winner_store, sub_batch_id, load_contract(contract_file)
        )
        sub_synthetic_rows = synthetic_missing_issues(winner_store, sub_batch_id)
        preexisting = [
            row for row in sub_synthetic_rows
            if (row["device_id"], row["point_key"], row["issue_code"],
                row["quality_code"], row["ts"]) in original_synthetic_ids
        ]
        induced = [
            row for row in sub_synthetic_rows
            if (row["device_id"], row["point_key"], row["issue_code"],
                row["quality_code"], row["ts"]) not in original_synthetic_ids
        ]
        target_present = []
        for identity in sorted(target_identities):
            row = find_issue_row(
                winner_store, sub_batch_id,
                device_id=identity[0], point_key=identity[1],
                issue_code=identity[2], quality_code=identity[3], ts=identity[4],
            )
            target_present.append({
                "device_id": identity[0],
                "point_key": identity[1],
                "issue_code": identity[2],
                "quality_code": identity[3],
                "ts": identity[4],
                "sub_line_no": int(row["line_no"]) if row else None,
                "synthetic": bool(row and row["line_no"] == SYNTHETIC_LINE_NO),
            })
        line_mapping = [
            {"sub_line_no": new_no, "original_line_no": original_no}
            for new_no, original_no in enumerate(winner, start=1)
        ]
        guarantee = (
            "exact: all subsets were enumerated by increasing cardinality and "
            "original line-number order; the first feasible subset is the unique "
            "shortest, lexicographically smallest answer"
        )
        result = MinReplayResult(
            status="optimal",
            mode=mode,
            guarantee=guarantee,
            original_replay_path=str(replay_file.resolve()),
            original_replay_sha256=actual_replay_sha,
            sub_replay_sha256=winner_payload_sha,
            contract_sha256=contract_sha,
            original_batch_id=batch_id,
            sub_batch_id=sub_batch_id,
            total_input_lines=n_lines,
            sub_line_count=len(winner),
            targets=target_rows,
            target_present=target_present,
            line_mapping=line_mapping,
            report_diff=diff_summaries(original_summary, sub_summary),
            synthetic_missing={
                "preexisting_from_original_batch": _synthetic_payload(preexisting),
                "induced_by_subset": _synthetic_payload(induced),
            },
            minimality={
                "search": "exact_exhaustive",
                "max_input_lines": int(max_input_lines),
                "input_lines": n_lines,
                "subsets_enumerated": subsets_enumerated,
                "subsets_replayed": subsets_replayed,
                "subsets_pruned": subsets_pruned,
                "shortest_line_count": len(winner),
                "tie_break": "original line-number sequence, lexicographic order",
                "winning_original_lines": list(winner),
            },
            sub_replay_bytes=b"".join(
                physical_lines[index - 1] for index in winner
            ),
        )
        return result
    finally:
        if winner_store is not None:
            winner_store.close()
        if winner_run_dir is not None:
            shutil.rmtree(winner_run_dir, ignore_errors=True)
        shutil.rmtree(work_root, ignore_errors=True)


def build_proof(result: MinReplayResult) -> dict[str, Any]:
    """Stable, JSON-serializable verification proof for one result."""
    return {
        "schema_version": PROOF_SCHEMA_VERSION,
        "status": result.status,
        "preservation_mode": result.mode,
        "guarantee": result.guarantee,
        "original_replay": {
            "path": result.original_replay_path,
            "sha256": result.original_replay_sha256,
            "line_count": result.total_input_lines,
            "batch_id": result.original_batch_id,
        },
        "sub_replay": {
            "sha256": result.sub_replay_sha256,
            "line_count": result.sub_line_count,
            "batch_id": result.sub_batch_id,
        },
        "contract_sha256": result.contract_sha256,
        "targets": result.targets,
        "target_present": result.target_present,
        "line_mapping": result.line_mapping,
        "synthetic_missing": result.synthetic_missing,
        "report_diff": result.report_diff,
        "minimality": result.minimality,
    }


def write_min_replay_bundle(
    result: MinReplayResult, output_dir: str | Path
) -> dict[str, str]:
    """Write the sub-replay and its byte-stable proof into ``output_dir``.

    The original batch, replay and contract are never modified; all outputs are
    newly created files under the given directory.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    sub_path = atomic_write_bytes(out / "sub_replay.jsonl", result.sub_replay_bytes)
    # The on-disk digest must match the proof's payload digest.
    if sha256_file(sub_path) != result.sub_replay_sha256:  # pragma: no cover
        raise MinReplayError(
            "proof_digest_mismatch",
            "written sub-replay digest does not match the recorded sha256",
        )
    proof_text = json.dumps(
        build_proof(result), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    proof_path = atomic_write_text(out / "proof.json", proof_text)
    return {
        "sub_replay": str(sub_path),
        "proof": str(proof_path),
        "sub_replay_sha256": result.sub_replay_sha256,
        "proof_sha256": sha256_text(proof_text),
    }
