"""End-to-end mode-state review against a real ledger and reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pointlog.codec import encode_raw
from pointlog.contract import ContractSet, load_contract, parse_contract
from pointlog.mode_state import (
    EVIDENCE_DIRECT,
    EVIDENCE_IMPOSSIBLE,
    EVIDENCE_UNPROVEN,
    ModeReviewError,
    mode_state_review,
)
from pointlog.modbus import build_request, build_response
from pointlog.pipeline import IngestInterrupted, run_capture
from pointlog.store import Store


CONTRACT = """version: 1
devices:
  - id: rig
    label: Synthetic rig
    points:
      - key: mode
        slave_id: 7
        function: 3
        start: 10
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        range: [0, 3]
        enum:
          0: idle
          1: warmup
          2: run
          3: fault
        mode_state:
{block}
"""

HOP = """          verification: hop
          max_gap_seconds: 0
          min_dwell_seconds:
            1: 120
          reset_state: 3
          transitions:
            0: [1, 3]
            1: [2, 3]
            2: [0, 3]
            3: [0]
"""

BOUNDED = HOP.replace("verification: hop", "verification: bounded").replace(
    "max_gap_seconds: 0", "max_gap_seconds: 900"
)

REQUEST = build_request(7, 3, 10, 1).hex()


def _line(ts: str, raw: int, *, response: str | None = None) -> dict:
    payload = build_response(7, 3, encode_raw(raw, "uint16", "AB")).hex()
    return {
        "ts": ts,
        "request_hex": REQUEST,
        "response_hex": payload if response is None else response,
        "source": "rig",
    }


def _write_replay(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def _write_contract(path: Path, block: str = HOP) -> None:
    path.write_text(CONTRACT.format(block=block), encoding="utf-8")


def _run(tmp_path: Path, block: str, records: list[dict], name: str = "r"):
    replay = tmp_path / f"{name}.jsonl"
    contract = tmp_path / f"{name}.yaml"
    db = tmp_path / f"{name}.db"
    reports = tmp_path / f"{name}-reports"
    _write_replay(replay, records)
    _write_contract(contract, block)
    result = run_capture(
        replay_path=replay, contract_path=contract, db_path=db, reports_dir=reports,
    )
    return result, db, reports, contract


def _review(db: Path, contract: ContractSet):
    store = Store(db, read_only=True)
    try:
        return mode_state_review(store, store.conn.execute(
            "SELECT id FROM batches"
        ).fetchone()[0], contract)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The acceptance scenario: A at 00:00, C at 00:10
# ---------------------------------------------------------------------------


def test_hop_semantics_reports_path_violation_and_process_log(tmp_path):
    _, db, reports, contract_path = _run(
        tmp_path, HOP,
        [_line("2025-03-01T00:00:00Z", 0), _line("2025-03-01T00:10:00Z", 2)],
    )
    review = _review(db, load_contract(contract_path))[0]
    assert review["verification"] == "hop"
    assert review["summary"]["direct"] == 0
    assert review["summary"]["unproven"] == 1
    assert review["summary"]["impossible"] == 0
    pair = review["transitions"][0]
    assert pair["grade"] == EVIDENCE_UNPROVEN
    assert pair["verdict"] == "hop_not_observed"
    assert pair["assumed_path"] == [0, 1, 2]
    assert "流程日志" in pair["recommendation"]
    # The report must not claim the device legally ran.
    markdown = (reports / "summary.md").read_text(encoding="utf-8")
    assert "枚举状态时序复核" in markdown
    assert "hop_not_observed" in markdown
    assert "0→1→2" in markdown
    assert "流程日志" in markdown


def test_bounded_semantics_reports_windows_and_asks_for_evidence(tmp_path):
    _, db, reports, contract_path = _run(
        tmp_path, BOUNDED,
        [_line("2025-03-01T00:00:00Z", 0), _line("2025-03-01T00:10:00Z", 2)],
    )
    review = _review(db, load_contract(contract_path))[0]
    assert review["verification"] == "bounded"
    pair = review["transitions"][0]
    assert pair["grade"] == EVIDENCE_UNPROVEN
    assert pair["verdict"] == "missed_intermediate"
    assert pair["assumed_path"] == [0, 1, 2]
    assert pair["min_required_seconds"] == 120
    window = pair["plausible_windows"][0]
    assert window["earliest_ts"] == "2025-03-01T00:00:00Z"
    assert window["latest_ts"] == "2025-03-01T00:08:00Z"
    assert "补充采集" in pair["recommendation"]
    markdown = (reports / "summary.md").read_text(encoding="utf-8")
    assert "missed_intermediate" in markdown
    assert "2025-03-01T00:08:00Z" in markdown
    assert "补充采集" in markdown


def test_same_input_produces_different_triage_actions(tmp_path):
    records = [_line("2025-03-01T00:00:00Z", 0), _line("2025-03-01T00:10:00Z", 2)]
    _, hop_db, hop_reports, hop_contract = _run(tmp_path, HOP, records, name="hop")
    _, bounded_db, bounded_reports, bounded_contract = _run(
        tmp_path, BOUNDED, records, name="bounded"
    )
    hop = _review(hop_db, load_contract(hop_contract))[0]["transitions"][0]
    bounded = _review(
        bounded_db, load_contract(bounded_contract)
    )[0]["transitions"][0]
    assert hop["grade"] == bounded["grade"] == EVIDENCE_UNPROVEN
    assert hop["recommendation"] != bounded["recommendation"]
    assert "流程日志" in hop["recommendation"]
    assert "补充采集" in bounded["recommendation"]
    assert (hop_reports / "summary.json").read_bytes() != (
        bounded_reports / "summary.json"
    ).read_bytes()


def test_insufficient_dwell_is_impossible_under_both_semantics(tmp_path):
    records = [_line("2025-03-01T00:00:00Z", 0), _line("2025-03-01T00:01:00Z", 2)]
    for block, name in ((HOP, "hop"), (BOUNDED, "bounded")):
        _, db, reports, contract_path = _run(tmp_path, block, records, name=name)
        review = _review(db, load_contract(contract_path))[0]
        pair = review["transitions"][0]
        assert pair["grade"] == EVIDENCE_IMPOSSIBLE
        assert pair["verdict"] == "impossible"
        assert pair["min_required_seconds"] == 120
        assert review["summary"]["impossible"] == 1
        markdown = (reports / "summary.md").read_text(encoding="utf-8")
        assert "| impossible |" in markdown or "impossible" in markdown


def test_direct_legal_sequence_is_confirmed(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:02:00Z", 1),
        _line("2025-03-01T00:09:00Z", 1),
        _line("2025-03-01T00:10:00Z", 2),
    ]
    _, db, reports, contract_path = _run(tmp_path, BOUNDED, records)
    review = _review(db, load_contract(contract_path))[0]
    assert review["summary"]["impossible"] == review["summary"]["unproven"] == 0
    assert review["summary"]["unknown_segments"] == 0
    pairs = review["transitions"]
    pairs = [pair for pair in pairs if pair["from_state"]["raw_value"]
             != pair["to_state"]["raw_value"]]
    assert [(pair["from_state"]["raw_value"], pair["to_state"]["raw_value"])
            for pair in pairs] == [(0, 1), (1, 2)]
    assert all(pair["grade"] == EVIDENCE_DIRECT for pair in pairs)


# ---------------------------------------------------------------------------
# Reset semantics
# ---------------------------------------------------------------------------


def test_reset_requires_an_explicit_legal_edge(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 2),   # run
        _line("2025-03-01T00:01:00Z", 3),   # fault = reset, declared edge 2->3
    ]
    _, db, _, contract_path = _run(tmp_path, HOP, records)
    pair = _review(db, load_contract(contract_path))[0]["transitions"][0]
    assert pair["grade"] == EVIDENCE_DIRECT
    assert pair["via_reset"] is True


def test_undeclared_jump_to_reset_is_not_called_reset(tmp_path):
    # Only 2 -> 3 enters the reset state, so an observed 0 -> 3 is not a reset
    # edge even though 3 is declared as the reset state.
    block = (HOP.replace("            0: [1, 3]", "            0: [1]")
             .replace("            1: [2, 3]", "            1: [2]"))
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:01:00Z", 3),
    ]
    _, db, _, contract_path = _run(tmp_path, block, records)
    pair = _review(db, load_contract(contract_path))[0]["transitions"][0]
    assert pair["grade"] == EVIDENCE_IMPOSSIBLE
    assert pair["verdict"] == "impossible"
    assert pair["via_reset"] is False
    assert pair["assumed_path"] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Contaminated inputs
# ---------------------------------------------------------------------------


def test_same_timestamp_conflict_is_unknown_not_ordered(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:01:00Z", 2),
        _line("2025-03-01T00:01:00Z", 1),
        _line("2025-03-01T00:10:00Z", 3),
    ]
    _, db, reports, contract_path = _run(tmp_path, BOUNDED, records)
    review = _review(db, load_contract(contract_path))[0]
    # The 00:01 pair is contradictory, but a third, distinct observation at
    # 00:00 and 00:10 still exists, so two confirmed observations remain.
    assert review["summary"]["observations"] == 2
    unknown = [item for item in review["segments"] if item["kind"] == "unknown"]
    assert len(unknown) == 1
    assert "same_time_conflict" in unknown[0]["reasons"]
    assert set(unknown[0]["line_numbers"]) >= {2, 3}
    # Neither conflicting row is used to claim a legal transition.
    states = [
        (pair["from_state"]["raw_value"], pair["to_state"]["raw_value"])
        for pair in review["transitions"]
    ]
    assert (0, 1) not in states and (0, 2) not in states


def test_bad_frame_and_long_gap_merge_into_one_segment(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:05:00Z", 1, response="deadbeef"),
        _line("2025-03-01T00:07:00Z", 1, response=""),
        _line("2025-03-01T00:20:00Z", 2),
    ]
    _, db, reports, contract_path = _run(tmp_path, BOUNDED, records)
    review = _review(db, load_contract(contract_path))[0]
    unknown = [item for item in review["segments"] if item["kind"] == "unknown"]
    assert len(unknown) == 1, unknown
    assert unknown[0]["start_ts"] == "2025-03-01T00:00:00Z"
    assert unknown[0]["end_ts"] == "2025-03-01T00:20:00Z"
    assert "long_gap" in unknown[0]["reasons"]
    markdown = (reports / "summary.md").read_text(encoding="utf-8")
    # One unknown stretch, not one fault per bad row.
    assert markdown.count("| 未知 |") == 1


def test_out_of_order_lines_are_sorted_by_timestamp(tmp_path):
    records = [
        _line("2025-03-01T00:10:00Z", 2),
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:02:00Z", 1),
    ]
    _, db, _, contract_path = _run(tmp_path, HOP, records)
    review = _review(db, load_contract(contract_path))[0]
    assert review["out_of_order_input"] is True
    assert [
        (pair["from_state"]["raw_value"], pair["to_state"]["raw_value"])
        for pair in review["transitions"]
    ] == [(0, 1), (1, 2)]


# ---------------------------------------------------------------------------
# Batch lifecycle and immutability
# ---------------------------------------------------------------------------


def test_incomplete_batch_is_rejected(tmp_path):
    replay = tmp_path / "r.jsonl"
    contract = tmp_path / "r.yaml"
    db = tmp_path / "r.db"
    _write_replay(replay, [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:10:00Z", 2),
    ])
    _write_contract(contract, HOP)
    with pytest.raises(IngestInterrupted):
        run_capture(replay_path=replay, contract_path=contract, db_path=db,
                    reports_dir=None, stop_after=1)
    store = Store(db, read_only=True)
    try:
        batch_id = store.conn.execute("SELECT id FROM batches").fetchone()[0]
        with pytest.raises(ModeReviewError) as excinfo:
            mode_state_review(store, batch_id, load_contract(contract))
        assert excinfo.value.code == "batch_not_completed"
    finally:
        store.close()


def test_resume_produces_identical_review(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:02:00Z", 1),
        _line("2025-03-01T00:10:00Z", 2),
    ]
    replay = tmp_path / "r.jsonl"
    contract = tmp_path / "r.yaml"
    db = tmp_path / "r.db"
    _write_replay(replay, records)
    _write_contract(contract, BOUNDED)
    with pytest.raises(IngestInterrupted):
        run_capture(replay_path=replay, contract_path=contract, db_path=db,
                    reports_dir=None, stop_after=2)
    run_capture(replay_path=replay, contract_path=contract, db_path=db,
                reports_dir=tmp_path / "resumed", resume=True)

    # A clean full run must match the resumed one exactly.
    full_db = tmp_path / "full.db"
    result = run_capture(replay_path=replay, contract_path=contract, db_path=full_db,
                         reports_dir=tmp_path / "full")
    full_review = _review(full_db, load_contract(contract))
    store = Store(db, read_only=True)
    try:
        batch_id = store.conn.execute("SELECT id FROM batches").fetchone()[0]
        resumed_review = mode_state_review(store, batch_id, load_contract(contract))
    finally:
        store.close()
    assert resumed_review == full_review
    assert result.status == "completed"


def test_review_does_not_modify_readings_or_issues(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:10:00Z", 2),
    ]
    result, db, reports, contract_path = _run(tmp_path, BOUNDED, records)
    store = Store(db, read_only=True)
    try:
        before = {
            "readings": [tuple(row) for row in store.conn.execute(
                "SELECT * FROM readings ORDER BY id")],
            "issues": [tuple(row) for row in store.conn.execute(
                "SELECT * FROM issues ORDER BY id")],
        }
    finally:
        store.close()
    # Re-render reports (which triggers the review) and compare the ledger.
    from pointlog.report import write_reports
    store = Store(db, read_only=True)
    try:
        write_reports(store, result.batch_id, load_contract(contract_path), reports)
        after = {
            "readings": [tuple(row) for row in store.conn.execute(
                "SELECT * FROM readings ORDER BY id")],
            "issues": [tuple(row) for row in store.conn.execute(
                "SELECT * FROM issues ORDER BY id")],
        }
    finally:
        store.close()
    assert after == before


def test_review_is_byte_stable_across_repeated_renders(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:10:00Z", 2),
    ]
    result, db, reports, contract_path = _run(tmp_path, BOUNDED, records)
    first = (reports / "summary.json").read_bytes()
    from pointlog.report import write_reports
    store = Store(db, read_only=True)
    try:
        write_reports(store, result.batch_id, load_contract(contract_path), reports)
        second = (reports / "summary.json").read_bytes()
    finally:
        store.close()
    assert first == second


def test_html_report_contains_evidence_tables(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:10:00Z", 2),
    ]
    _, db, reports, contract_path = _run(tmp_path, BOUNDED, records)
    html = (reports / "summary.html").read_text(encoding="utf-8")
    assert "枚举状态时序复核" in html
    assert "missed_intermediate" in html
    assert "2025-03-01T00:08:00Z" in html
    # Still self-contained and script-free.
    assert "<script" not in html
    assert "http://" not in html and "https://" not in html


def test_json_mode_review_shape_is_stable(tmp_path):
    records = [
        _line("2025-03-01T00:00:00Z", 0),
        _line("2025-03-01T00:10:00Z", 2),
    ]
    _, db, reports, contract_path = _run(tmp_path, BOUNDED, records)
    summary = json.loads((reports / "summary.json").read_text(encoding="utf-8"))
    [review] = summary["mode_state_review"]
    assert list(review) == [
        "device_id", "point_key", "verification", "max_gap_seconds",
        "reset_state", "states", "transitions_declared", "min_dwell_seconds",
        "out_of_order_input", "ignored", "segments", "transitions", "summary",
    ]
    [pair] = review["transitions"]
    for key in ("from_ts", "to_ts", "from_line", "to_line", "grade", "verdict",
                "assumed_path", "min_required_seconds", "plausible_windows",
                "via_reset", "detail", "recommendation", "separated_by_unknown"):
        assert key in pair


def test_report_omits_section_without_mode_state(tmp_path):
    contract = tmp_path / "plain.yaml"
    contract.write_text(
        CONTRACT.format(block=HOP).replace(
            """        mode_state:
          verification: hop
          max_gap_seconds: 0
          min_dwell_seconds:
            1: 120
          reset_state: 3
          transitions:
            0: [1, 3]
            1: [2, 3]
            2: [0, 3]
            3: [0]
""",
            "",
        ),
        encoding="utf-8",
    )
    replay = tmp_path / "r.jsonl"
    _write_replay(replay, [_line("2025-03-01T00:00:00Z", 0)])
    reports = tmp_path / "reports"
    run_capture(replay_path=replay, contract_path=contract,
                db_path=tmp_path / "r.db", reports_dir=reports)
    summary = json.loads((reports / "summary.json").read_text(encoding="utf-8"))
    assert "mode_state_review" not in summary
    assert "枚举状态时序复核" not in (reports / "summary.md").read_text(
        encoding="utf-8")
