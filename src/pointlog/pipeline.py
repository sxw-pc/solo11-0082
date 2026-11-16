"""Capture orchestration: validation, batching, resume, persistence, reports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contract import ContractSet, load_contract
from .lineproc import PAIRING_ROUTES, EventPairer, process_line
from .quality import severity
from .replay import ReplaySource, load_replay
from .resume import ResumePlan, plan_resume
from .store import Store, make_batch_id
from .store_queries import batch_totals
from .util import sha256_file, sha256_text, utc_now_iso


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
    replay_mode: str = "pairs"
    pairing_route: str = ""
    report_paths: dict[str, str] = field(default_factory=dict)


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
        replay_mode=str(batch.get("replay_mode") or "pairs"),
        pairing_route=str(batch.get("pairing_route") or ""),
        report_paths=report_paths or {},
    )


def _select_initial_batch(
    store: Store, replay: ReplaySource, contract_path: Path,
    contract_sha256: str, now: str, *,
    replay_mode: str, pairing_route: str, pair_timeout_ms: int,
    mode_fingerprint: str,
) -> tuple[dict[str, Any], bool]:
    batch_id = make_batch_id(replay.sha256, contract_sha256, mode_fingerprint)
    existing = store.find_batch(replay.sha256, contract_sha256, mode_fingerprint)
    if existing is None:
        batch = store.create_batch(
            batch_id=batch_id,
            replay_path=str(replay.path.resolve()),
            replay_sha256=replay.sha256,
            contract_path=str(contract_path),
            contract_sha256=contract_sha256,
            started_at=now,
            total_lines=replay.total_lines,
            replay_mode=replay_mode,
            pairing_route=pairing_route if replay_mode == "events" else "",
            pair_timeout_ms=pair_timeout_ms if replay_mode == "events" else 0,
            mode_fingerprint=mode_fingerprint,
        )
        store.ensure_resume_state(
            replay_path=str(replay.path.resolve()),
            replay_sha256=replay.sha256,
            batch_id=batch_id,
            total_lines=replay.total_lines,
            updated_at=now,
            mode_fingerprint=mode_fingerprint,
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
    store: Store, replay: ReplaySource, contract_sha256: str, now: str,
    mode_fingerprint: str,
) -> tuple[ResumePlan, bool]:
    plan = plan_resume(store, replay, contract_sha256=contract_sha256, now=now,
                       mode_fingerprint=mode_fingerprint)
    return plan, plan.batch["status"] == "completed"


def _process_paired_lines(
    store: Store,
    replay: ReplaySource,
    contract: ContractSet,
    batch_id: str,
    *,
    offset: int,
    seen_points: set[str],
    stop_after: int | None,
    now_factory: Callable[[], str],
) -> int:
    processed_offset = offset
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
    return processed_offset


def _process_event_lines(
    store: Store,
    replay: ReplaySource,
    contract: ContractSet,
    batch_id: str,
    *,
    offset: int,
    pairing_route: str,
    pair_timeout_ms: int,
    seen_points: set[str],
    stop_after: int | None,
    now_factory: Callable[[], str],
) -> int:
    """Run the split-frame pairing machine, committing one event per transaction.

    Lines at or below ``offset`` are replayed without persistence to rebuild
    the exact pairing state (open requests, timed-out late candidates, used
    pair identifiers) as of the resume boundary; the file fingerprint is
    verified before resume, so the rebuilt state is identical.
    """
    pairer = EventPairer(
        store=store,
        batch_id=batch_id,
        contract=contract,
        route=pairing_route,
        pair_timeout_ms=pair_timeout_ms,
        seen_points=seen_points,
    )
    state_key = (str(replay.path.resolve()), replay.sha256)
    processed_offset = offset
    for event in replay.iter_events(0):
        outcome = pairer.handle_event(event)
        if event.line_no <= offset:
            continue  # state rebuild only; already committed by the partial run
        store.save_event_outcomes(
            batch_id=batch_id,
            event_row=outcome.event_row,
            event_updates=outcome.event_updates,
            frames=outcome.frames,
            issues=outcome.issues,
            reading=outcome.reading,
            state_key=state_key,
            line_offset=event.line_no,
            total_lines=replay.total_lines,
            updated_at=now_factory(),
        )
        processed_offset = event.line_no
        if stop_after is not None and processed_offset - offset >= stop_after:
            raise IngestInterrupted(batch_id, processed_offset)
    final = pairer.finalize()
    store.save_event_outcomes(
        batch_id=batch_id,
        event_row=final.event_row,
        event_updates=final.event_updates,
        frames=final.frames,
        issues=final.issues,
        reading=final.reading,
        state_key=state_key,
        line_offset=replay.total_lines,
        total_lines=replay.total_lines,
        updated_at=now_factory(),
    )
    return processed_offset


def run_capture(
    *,
    replay_path: str | Path,
    contract_path: str | Path,
    db_path: str | Path,
    reports_dir: str | Path | None,
    resume: bool = False,
    stop_after: int | None = None,
    now_factory: Callable[[], str] = utc_now_iso,
    replay_mode: str = "pairs",
    pairing_route: str = "explicit",
    pair_timeout_ms: int = 1000,
) -> RunResult:
    """Run one capture. Contract validation happens before the ledger is opened."""
    if stop_after is not None and stop_after <= 0:
        raise ValueError("stop_after must be a positive integer")
    if replay_mode not in ("pairs", "events"):
        raise ValueError("replay_mode must be 'pairs' or 'events'")
    if replay_mode == "events":
        if pairing_route not in PAIRING_ROUTES:
            raise ValueError(f"pairing_route must be one of {PAIRING_ROUTES}")
        if pair_timeout_ms <= 0:
            raise ValueError("pair_timeout_ms must be a positive integer")
    contract_path = Path(contract_path)
    contract = load_contract(contract_path)
    replay = load_replay(replay_path)
    contract_sha256 = sha256_file(contract_path)
    mode_fingerprint = ""
    if replay_mode == "events":
        mode_fingerprint = sha256_text(
            f"events|{pairing_route}|{pair_timeout_ms}"
        )[:12]
    now = now_factory()
    store = Store(db_path)
    try:
        if resume:
            plan, already_complete = _resume_batch(
                store, replay, contract_sha256, now, mode_fingerprint
            )
            batch = plan.batch
            offset = plan.offset
            resumed = True
            reused = already_complete
        else:
            batch, already_complete = _select_initial_batch(
                store, replay, contract_path, contract_sha256, now,
                replay_mode=replay_mode, pairing_route=pairing_route,
                pair_timeout_ms=pair_timeout_ms, mode_fingerprint=mode_fingerprint,
            )
            offset = int(batch["processed_lines"]) if already_complete else 0
            resumed = False
            reused = already_complete

        batch_id = batch["id"]
        seen_points = store.seen_contract_points(batch_id)
        processed_offset = offset
        if not already_complete:
            try:
                if replay_mode == "events":
                    processed_offset = _process_event_lines(
                        store, replay, contract, batch_id,
                        offset=offset, pairing_route=pairing_route,
                        pair_timeout_ms=pair_timeout_ms, seen_points=seen_points,
                        stop_after=stop_after, now_factory=now_factory,
                    )
                else:
                    processed_offset = _process_paired_lines(
                        store, replay, contract, batch_id,
                        offset=offset, seen_points=seen_points,
                        stop_after=stop_after, now_factory=now_factory,
                    )
                _missing_contract_issues(store, batch_id, contract, seen_points)
                store.refresh_batch_stats(
                    batch_id,
                    status="completed",
                    processed_lines=replay.total_lines,
                    total_lines=replay.total_lines,
                    finished_at=now_factory(),
                )
                processed_offset = replay.total_lines
            except IngestInterrupted as exc:
                store.refresh_batch_stats(
                    batch_id,
                    status="partial",
                    processed_lines=exc.processed_lines,
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
        store.close()
