"""Resume-state validation, fingerprint checks and offset reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .replay import ReplaySource
from .store import Store


class ResumeRejected(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ResumePlan:
    batch: dict[str, Any]
    offset: int
    state_found: bool
    state_repaired: bool


def plan_resume(
    store: Store,
    replay: ReplaySource,
    *,
    contract_sha256: str,
    now: str,
) -> ResumePlan:
    replay_path = str(replay.path.resolve())
    current_sha = replay.sha256
    state = store.get_resume_state(replay_path, current_sha)
    path_states = store.list_resume_states(replay_path)

    if state is not None:
        batch = store.get_batch(state["batch_id"])
        if batch is None:
            raise ResumeRejected(
                "resume_state_corrupt", "resume_state references a missing batch"
            )
        if batch["replay_sha256"] != current_sha:
            raise ResumeRejected(
                "resume_state_corrupt", "resume_state and batch fingerprints disagree"
            )
        if batch["contract_sha256"] != contract_sha256:
            raise ResumeRejected(
                "contract_changed",
                "contract changed since the partial batch; create a new batch",
            )
        offset = int(state["line_offset"])
        if offset > replay.total_lines:
            raise ResumeRejected(
                "resume_state_corrupt",
                f"stored offset {offset} exceeds replay lines {replay.total_lines}",
            )
        max_line = store.max_processed_line(batch["id"])
        repaired = False
        if max_line > offset:
            # The state row may have been lost/stale while frame commits survived.
            offset = max_line
            store.set_resume_state(
                replay_path=replay_path,
                replay_sha256=current_sha,
                batch_id=batch["id"],
                line_offset=offset,
                total_lines=replay.total_lines,
                updated_at=now,
            )
            repaired = True
        return ResumePlan(batch=batch, offset=offset, state_found=True,
                          state_repaired=repaired)

    if path_states:
        latest = path_states[0]
        raise ResumeRejected(
            "replay_changed",
            "replay file fingerprint differs from the recorded resume state "
            f"({current_sha[:12]} != {str(latest['replay_sha256'])[:12]}); "
            "create a new batch instead of resuming",
        )

    batch = store.find_latest_batch_for_path(replay_path)
    if batch is None:
        raise ResumeRejected(
            "no_resume_state", "no resume state or prior batch exists for this replay path"
        )
    if batch["replay_sha256"] != current_sha:
        raise ResumeRejected(
            "replay_changed",
            "replay file fingerprint differs from the prior batch; create a new batch",
        )
    if batch["contract_sha256"] != contract_sha256:
        raise ResumeRejected(
            "contract_changed",
            "contract fingerprint differs from the prior batch; create a new batch",
        )
    offset = min(store.max_processed_line(batch["id"]), replay.total_lines)
    store.set_resume_state(
        replay_path=replay_path,
        replay_sha256=current_sha,
        batch_id=batch["id"],
        line_offset=offset,
        total_lines=replay.total_lines,
        updated_at=now,
    )
    return ResumePlan(batch=batch, offset=offset, state_found=False,
                      state_repaired=True)
