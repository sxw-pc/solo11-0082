"""Capture orchestration: validation, batching, resume, persistence, reports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import ContractSet, load_contract
from .lineproc import process_line
from .quality import severity
from .replay import (
    SEAL_ROUTE,
    ReplayError,
    ReplaySource,
    load_replay,
    sealed_copy_path,
    source_drift_status,
    verify_copy,
)
from .resume import ResumePlan, ResumeRejected, plan_resume
from .store import Store, make_batch_id
from .store_queries import batch_totals
from .util import sha256_file, utc_now_iso


class PipelineError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class IngestInterrupted(RuntimeError):
    def __init__(self, batch_id: str, processed_lines: int):
        super().__init__(f"ingest interrupted after {processed_lines} lines")
        self.batch_id = batch_id
        self.processed_lines = processed_lines


@dataclass
class RunResult:
    batch_id: str
    status: str
    resumed: bool
    reused: bool
    processed_lines: int
    total_lines: int
    reading_count: int
    valid_readings: int
    issue_count: int
    pending_points: int
    report_paths: dict[str, str] = field(default_factory=dict)
    version_fence: dict[str, Any] = field(default_factory=dict)


def _missing_contract_issues(
    store: Store,
    batch_id: str,
    contract: ContractSet,
    seen_points: set[str],
) -> None:
    for point in sorted(contract.points, key=lambda item: (item.device_id, item.point_key)):
        if point.point_key in seen_points:
            continue
        store.add_issue(batch_id, {
            "line_no": 0,
            "device_id": point.device_id,
            "point_key": point.point_key,
            "issue_code": "missing_response",
            "quality_code": "missing_response",
            "severity": severity("missing_response"),
            "frame_summary": "<no frame>",
            "detail": "contract point has no request/response in this replay",
            "ts": "",
            "pending_collect": 1,
        })


def _result(store: Store, batch_id: str, *, resumed: bool, reused: bool,
            processed_lines: int, total_lines: int,
            report_paths: dict[str, str] | None = None) -> RunResult:
    batch = store.get_batch(batch_id)
    if batch is None:  # pragma: no cover - defensive
        raise PipelineError("batch_missing", f"batch {batch_id} disappeared")
    totals = batch_totals(store, batch_id)
    return RunResult(
        batch_id=batch_id,
        status=batch["status"],
        resumed=resumed,
        reused=reused,
        processed_lines=processed_lines,
        total_lines=total_lines,
        reading_count=totals["readings"],
        valid_readings=totals["valid_readings"],
        issue_count=totals["issues"],
        pending_points=totals["pending_points"],
        report_paths=report_paths or {},
        version_fence=store.get_fence(batch_id) or {},
    )


def _select_initial_batch(
    store: Store, replay: ReplaySource, contract_path: Path,
    contract_sha256: str, now: str,
) -> tuple[dict[str, Any], bool]:
    batch_id = make_batch_id(replay.sha256, contract_sha256)
    existing = store.find_batch(replay.sha256, contract_sha256)
    if existing is None:
        batch = store.create_batch(
            batch_id=batch_id,
            replay_path=str(replay.path.resolve()),
            replay_sha256=replay.sha256,
            contract_path=str(contract_path),
            contract_sha256=contract_sha256,
            started_at=now,
            total_lines=replay.total_lines,
        )
        store.ensure_resume_state(
            replay_path=str(replay.path.resolve()),
            replay_sha256=replay.sha256,
            batch_id=batch_id,
            total_lines=replay.total_lines,
            updated_at=now,
        )
        return batch, False
    if existing["status"] != "completed":
        raise PipelineError(
            "batch_partial",
            f"batch {batch_id} is {existing['status']}; rerun with --resume "
            "or use a new output database",
        )
    return existing, True


def _resume_batch(
    store: Store, replay: ReplaySource, contract_sha256: str, now: str
) -> tuple[ResumePlan, bool]:
    plan = plan_resume(store, replay, contract_sha256=contract_sha256, now=now)
    return plan, plan.batch["status"] == "completed"


def _default_seal_dir(db_path: Path) -> Path:
    """Sealed copies live next to the ledger they serve as evidence for."""
    return db_path.resolve().parent / "sealed"


def _record_fence(
    store: Store,
    batch_id: str,
    replay: ReplaySource,
    *,
    result: str,
    source_status: str,
    detail: str,
) -> None:
    store.record_fence(
        batch_id=batch_id,
        route=SEAL_ROUTE,
        source_path=str(replay.path.resolve()),
        sealed_path=str(replay.read_path) if replay.read_path is not None else "",
        sha256=replay.sha256,
        byte_count=replay.byte_count,
        total_lines=replay.total_lines,
        source_status=source_status,
        result=result,
        detail=detail,
    )


def _record_unproven(
    store: Store,
    batch: dict[str, Any] | None,
    fence: dict[str, Any] | None,
    sha256: str,
    total_lines: int,
    *,
    source_status: str,
    detail: str,
) -> None:
    """Mark a batch's fence as unproven without touching its other provenance."""
    if batch is None:
        return
    store.record_fence(
        batch_id=batch["id"],
        route=SEAL_ROUTE,
        source_path=(fence or {}).get("source_path") or batch["replay_path"],
        sealed_path=(fence or {}).get("sealed_path") or "",
        sha256=sha256,
        byte_count=(fence or {}).get("byte_count"),
        total_lines=total_lines,
        source_status=source_status,
        result="unproven",
        detail=detail,
    )


def _status_for_replay_error(exc: ReplayError) -> str:
    return {
        "replay_missing": "missing",
        "replay_read": "unreadable",
    }.get(exc.code, "changed")


def _resume_source(
    store: Store, replay_path: str | Path, seal_root: Path
) -> ReplaySource:
    """Resolve the immutable replay version a resume must continue from.

    The sealed route adopts the fingerprint recorded in the resume state (or
    in the latest batch for the path) and proves it against the private
    sealed copy — never against whatever bytes the source path currently
    holds.  If the sealed copy is missing or fails verification, the source
    path is re-sealed once and accepted only when it reproduces the recorded
    fingerprint exactly; otherwise the version cannot be proven and the
    resume is rejected with ``version_unproven``.
    """
    source_path = Path(replay_path)
    resolved = str(source_path.resolve())
    states = store.list_resume_states(resolved)
    batch: dict[str, Any] | None = None
    if states:
        sha256 = states[0]["replay_sha256"]
        total_lines = int(states[0]["total_lines"])
        batch = store.get_batch(states[0]["batch_id"])
    else:
        batch = store.find_latest_batch_for_path(resolved)
        if batch is None:
            raise ResumeRejected(
                "no_resume_state",
                "no resume state or prior batch exists for this replay path",
            )
        sha256 = batch["replay_sha256"]
        total_lines = int(batch["total_lines"])

    if batch is not None and batch["status"] == "completed":
        # Reuse of a finished batch: its version was proven at completion and
        # no further lines are read, so no re-verification is needed.
        return ReplaySource(path=source_path, sha256=sha256, total_lines=total_lines)

    fence = store.get_fence(batch["id"]) if batch is not None else None
    if fence is not None and fence["result"] == "violated":
        raise ResumeRejected(
            "version_unproven",
            f"batch {batch['id']} has a recorded version-fence violation "
            f"({fence['detail']}); its committed frames are diagnostic only "
            "and the batch can never be completed — use a new output database",
        )

    sealed_path = (
        Path(fence["sealed_path"])
        if fence is not None and fence["sealed_path"]
        else sealed_copy_path(seal_root, sha256)
    )
    if sealed_path.is_file():
        ok, detail, nbytes = verify_copy(
            sealed_path, sha256=sha256, total_lines=total_lines
        )
        if ok:
            return ReplaySource(
                path=source_path,
                sha256=sha256,
                total_lines=total_lines,
                byte_count=nbytes,
                read_path=sealed_path,
            )
    else:
        detail = f"sealed copy is missing: {sealed_path}"

    # Last resort: re-seal the current source and accept it only if it
    # reproduces the recorded fingerprint exactly.
    try:
        replay = load_replay(source_path, seal_root)
    except ReplayError as exc:
        _record_unproven(
            store, batch, fence, sha256, total_lines,
            source_status=_status_for_replay_error(exc),
            detail=f"{detail}; source cannot be re-sealed: {exc.detail}",
        )
        raise ResumeRejected(
            "version_unproven",
            f"cannot prove replay version {sha256[:12]}…: {detail}; "
            f"source cannot be re-sealed: {exc.detail}",
        ) from exc
    if replay.sha256 != sha256 or replay.total_lines != total_lines:
        _record_unproven(
            store, batch, fence, sha256, total_lines,
            source_status="changed",
            detail=f"{detail}; current source holds a different version",
        )
        raise ResumeRejected(
            "version_unproven",
            f"cannot prove replay version {sha256[:12]}…: {detail}; the "
            "current source holds a different version — refusing to "
            "attribute it to the old batch",
        )
    if batch is not None:
        _record_fence(
            store, batch["id"], replay,
            result="verified", source_status="match",
            detail="sealed copy re-created from the source during resume",
        )
    return replay


def run_capture(
    *,
    replay_path: str | Path,
    contract_path: str | Path,
    db_path: str | Path,
    reports_dir: str | Path | None,
    resume: bool = False,
    stop_after: int | None = None,
    now_factory: Callable[[], str] = utc_now_iso,
    seal_dir: str | Path | None = None,
) -> RunResult:
    """Run one capture. Contract validation happens before the ledger is opened.

    The replay input is pinned behind a version fence: it is sealed into a
    private immutable copy and the batch identity, line count, per-line
    content and reports all derive from that one byte version.
    """
    if stop_after is not None and stop_after <= 0:
        raise ValueError("stop_after must be a positive integer")
    contract_path = Path(contract_path)
    contract = load_contract(contract_path)
    contract_sha256 = sha256_file(contract_path)
    db_path = Path(db_path)
    seal_root = Path(seal_dir) if seal_dir is not None else _default_seal_dir(db_path)
    now = now_factory()
    store: Store | None = None
    try:
        if resume:
            store = Store(db_path)
            replay = _resume_source(store, replay_path, seal_root)
            plan, already_complete = _resume_batch(store, replay, contract_sha256, now)
            batch = plan.batch
            offset = plan.offset
            resumed = True
            reused = already_complete
        else:
            # Sealing happens before the ledger is opened so a replay-side
            # failure never creates a database.
            replay = load_replay(replay_path, seal_root)
            store = Store(db_path)
            batch, already_complete = _select_initial_batch(
                store, replay, contract_path, contract_sha256, now
            )
            offset = int(batch["processed_lines"]) if already_complete else 0
            resumed = False
            reused = already_complete
            if not already_complete:
                _record_fence(
                    store, batch["id"], replay,
                    result="verified", source_status="match",
                    detail="source sealed into a private copy before batch creation",
                )

        batch_id = batch["id"]
        seen_points = store.seen_contract_points(batch_id)
        processed_offset = offset
        if not already_complete:
            try:
                for line in replay.iter_lines(offset):
                    outcome = process_line(store, batch_id, contract, line, seen_points)
                    store.save_line(
                        batch_id=batch_id,
                        frame=outcome.frame,
                        issues=outcome.issues,
                        reading=outcome.reading,
                        state_key=(str(replay.path.resolve()), replay.sha256),
                        line_offset=line.line_no,
                        total_lines=replay.total_lines,
                        updated_at=now_factory(),
                    )
                    processed_offset = line.line_no
                    if stop_after is not None and processed_offset - offset >= stop_after:
                        raise IngestInterrupted(batch_id, processed_offset)
                # Fence gate: the bytes actually read must still be the bytes
                # the batch claims.  Only then may the batch complete.
                read_target = (
                    replay.read_path if replay.read_path is not None else replay.path
                )
                ok, detail, _nbytes = verify_copy(
                    read_target,
                    sha256=replay.sha256,
                    total_lines=replay.total_lines,
                )
                if not ok:
                    _record_fence(
                        store, batch_id, replay,
                        result="violated",
                        source_status=source_drift_status(replay.path, replay.sha256),
                        detail=detail,
                    )
                    raise PipelineError(
                        "version_fence_violated",
                        "sealed replay copy failed verification after "
                        f"processing: {detail}; committed frames are kept for "
                        "diagnostics but are not a trusted capture of "
                        f"{replay.sha256[:12]}…",
                    )
                _missing_contract_issues(store, batch_id, contract, seen_points)
                store.refresh_batch_stats(
                    batch_id,
                    status="completed",
                    processed_lines=replay.total_lines,
                    total_lines=replay.total_lines,
                    finished_at=now_factory(),
                )
                _record_fence(
                    store, batch_id, replay,
                    result="verified",
                    source_status=source_drift_status(replay.path, replay.sha256),
                    detail="processed from the sealed copy; source drift "
                           "checked at completion",
                )
                processed_offset = replay.total_lines
            except IngestInterrupted:
                store.refresh_batch_stats(
                    batch_id,
                    status="partial",
                    processed_lines=processed_offset,
                    total_lines=replay.total_lines,
                    finished_at=None,
                )
                raise
            except Exception:
                store.refresh_batch_stats(
                    batch_id,
                    status="failed",
                    processed_lines=processed_offset,
                    total_lines=replay.total_lines,
                    finished_at=now_factory(),
                )
                raise

        report_paths: dict[str, str] = {}
        if reports_dir is not None:
            from .report import write_reports

            report_paths = write_reports(
                store, batch_id, contract, Path(reports_dir)
            )
        return _result(
            store, batch_id, resumed=resumed, reused=reused,
            processed_lines=processed_offset, total_lines=replay.total_lines,
            report_paths=report_paths,
        )
    finally:
        if store is not None:
            store.close()
