"""Unit tests for the temporal mode-state engine (no ledger required)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pointlog.codec import encode_raw
from pointlog.contract import ContractError, load_contract, parse_contract
from pointlog.mode_state import (
    EVIDENCE_DIRECT,
    EVIDENCE_IMPOSSIBLE,
    EVIDENCE_UNPROVEN,
    analyse_pair,
    analyse_timeline,
    build_point_timeline,
    parse_ts,
)
from pointlog.modbus import build_request, build_response

CONFIG = Path(__file__).resolve().parents[1] / "config" / "points.yaml"


def _point(mode_yaml: str, enum: str = "0: idle\n          1: warmup\n          2: run\n          3: fault"):
    text = f"""
version: 1
devices:
  - id: rig
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
          {enum}
        mode_state:
{mode_yaml}
"""
    return parse_contract("".join(text.splitlines(keepends=True))).get("mode")


def _hop(max_gap: int = 0):
    return _point(f"""          verification: hop
          max_gap_seconds: {max_gap}
          min_dwell_seconds:
            1: 120
          reset_state: 3
          transitions:
            0: [1, 3]
            1: [2, 3]
            2: [0, 3]
            3: [0]
""")


def _bounded(max_gap: int = 900):
    return _point(f"""          verification: bounded
          max_gap_seconds: {max_gap}
          min_dwell_seconds:
            1: 120
          reset_state: 3
          transitions:
            0: [1, 3]
            1: [2, 3]
            2: [0, 3]
            3: [0]
""")


def _reading(line_no, ts, raw, quality="good", label=None):
    return {
        "line_no": line_no,
        "ts": ts,
        "raw_value": raw,
        "quality_code": quality,
        "enum_label": label,
    }


def _frame(line_no, ts, quality="good", response_hex="", issue_code=""):
    return {
        "line_no": line_no,
        "ts": ts,
        "quality_code": quality,
        "issue_code": issue_code,
        "detail": "",
        "response_hex": response_hex,
    }


def _timeline(point, readings, frames=()):
    return build_point_timeline(point, readings, frames)


def test_graph_state_contract_is_exposed():
    mode = _hop().mode_state
    assert mode.states == (0, 1, 2, 3)
    assert mode.targets(1) == (2, 3)
    assert mode.dwell(1) == 120
    assert mode.dwell(0) == 0
    assert mode.has_edge(0, 1) and not mode.has_edge(0, 2)
    assert mode.reset_state == 3
    assert mode.verification == "hop"


# ---------------------------------------------------------------------------
# The two authorised verification semantics
# ---------------------------------------------------------------------------


def test_hop_semantics_flags_unobserved_hop_even_when_time_allows():
    point = _hop()
    pair = analyse_pair(point.mode_state, point.enum, 0, 2, 600)
    assert pair["grade"] == EVIDENCE_UNPROVEN
    assert pair["verdict"] == "hop_not_observed"
    assert pair["assumed_path"] == [0, 1, 2]
    assert pair["min_required_seconds"] == 120
    assert "流程日志" in pair["recommendation"]
    assert "未证明合法运行" in pair["detail"] or "不能证明" in pair["recommendation"]


def test_bounded_semantics_gives_feasible_window_but_not_proof():
    point = _bounded()
    pair = analyse_pair(point.mode_state, point.enum, 0, 2, 600)
    assert pair["grade"] == EVIDENCE_UNPROVEN
    assert pair["verdict"] == "missed_intermediate"
    assert pair["assumed_path"] == [0, 1, 2]
    window = pair["plausible_windows"][0]
    assert window["state"] == 1
    assert window["earliest_offset_seconds"] == 0
    assert window["latest_offset_seconds"] == 480  # 600 - 120 dwell
    assert "补充采集" in pair["recommendation"]


def test_same_input_yields_different_decisions_under_the_two_semantics():
    hop = analyse_pair(_hop().mode_state, _hop().enum, 0, 2, 600)
    bounded = analyse_pair(_bounded().mode_state, _bounded().enum, 0, 2, 600)
    assert hop["grade"] == bounded["grade"] == EVIDENCE_UNPROVEN
    assert hop["recommendation"] != bounded["recommendation"]
    assert "流程日志" in hop["recommendation"]
    assert "补充采集" in bounded["recommendation"]


def test_insufficient_dwell_is_impossible_under_both_semantics():
    hop_point, bounded_point = _hop(), _bounded()
    for point in (hop_point, bounded_point):
        pair = analyse_pair(point.mode_state, point.enum, 0, 2, 60)
        assert pair["grade"] == EVIDENCE_IMPOSSIBLE
        assert pair["verdict"] == "impossible"
        assert pair["assumed_path"] == [0, 1, 2]
        assert pair["min_required_seconds"] == 120
        assert pair["plausible_windows"] == []


def test_direct_legal_edge_is_never_refuted_by_exit_dwell():
    point = _hop()
    ok = analyse_pair(point.mode_state, point.enum, 0, 1, 600)
    assert ok["grade"] == EVIDENCE_DIRECT and ok["verdict"] == "direct"
    assert ok["assumed_path"] == [0, 1]
    # The departure instant of a state is not known, so even a one-second hop
    # on a declared edge cannot prove a dwell violation.
    quick = analyse_pair(point.mode_state, point.enum, 1, 2, 1)
    assert quick["grade"] == EVIDENCE_DIRECT
    assert quick["min_required_seconds"] == 0


def test_same_state_is_direct_without_dwell_gate():
    point = _hop()
    pair = analyse_pair(point.mode_state, point.enum, 1, 1, 0)
    assert pair["grade"] == EVIDENCE_DIRECT
    assert pair["assumed_path"] == [1]


def test_no_path_is_impossible_not_reset():
    point = _point("""          verification: hop
          transitions:
            0: [1]
            1: [2]
            2: [0]
""", enum="0: idle\n          1: warmup\n          2: run")
    pair = analyse_pair(point.mode_state, point.enum, 0, 3, 10_000)
    # state 3 is not even declared: no legal path, and not a reset jump.
    assert pair["grade"] == EVIDENCE_IMPOSSIBLE
    assert pair["via_reset"] is False


def test_reset_must_be_an_explicit_legal_edge():
    point = _hop()
    # 2 -> 3 is declared, so this is a legal reset edge.
    pair = analyse_pair(point.mode_state, point.enum, 2, 3, 1)
    assert pair["grade"] == EVIDENCE_DIRECT
    assert pair["via_reset"] is True
    # 1 -> 3 is explicit as well; 3 is a reset target.
    pair2 = analyse_pair(point.mode_state, point.enum, 1, 3, 200)
    assert pair2["via_reset"] is True and pair2["grade"] == EVIDENCE_DIRECT
    # 0 -> 2 is not an edge; the path 0->1->3->0->1->2 exists only if the
    # graph allows it, but 0->1->2 is shorter and 2 is not the reset state.
    pair3 = analyse_pair(point.mode_state, point.enum, 0, 2, 600)
    assert pair3["via_reset"] is False


def test_bounded_gap_beyond_max_is_unproven_with_gap_reason():
    point = _bounded(max_gap=120)
    pair = analyse_pair(point.mode_state, point.enum, 0, 2, 600)
    assert pair["grade"] == EVIDENCE_UNPROVEN
    assert pair["verdict"] == "gap_exceeds_max_gap"


# ---------------------------------------------------------------------------
# Timeline construction
# ---------------------------------------------------------------------------


def test_timeline_orders_by_timestamp_not_input_order():
    point = _hop()
    readings = [
        _reading(1, "2025-03-01T00:10:00Z", 2, label="run"),
        _reading(2, "2025-03-01T00:00:00Z", 0, label="idle"),
    ]
    timeline = _timeline(point, readings)
    assert [row.line_no for row in timeline["observations"]] == [2, 1]
    assert timeline["out_of_order_input"] is True
    review = analyse_timeline(point, timeline)
    transition = review["transitions"][0]
    assert transition["from_line"] == 2 and transition["to_line"] == 1


def test_bad_frames_and_unreadable_enum_exclude_readings():
    point = _hop()
    frames = [
        _frame(5, "2025-03-01T00:05:00Z", quality="bad_frame",
               issue_code="bad_crc_response"),
    ]
    readings = [
        _reading(1, "2025-03-01T00:00:00Z", 0, label="idle"),
        _reading(3, "2025-03-01T00:10:00Z", 7, quality="not_in_enum"),
        _reading(4, "not-a-time", 0),
    ]
    timeline = _timeline(point, readings, frames)
    assert [row.line_no for row in timeline["observations"]] == [1]
    unknown_lines = {
        row.line_no for row in timeline["rows"] if row.kind == "unknown"
    }
    assert unknown_lines == {3, 5}
    # An unparseable timestamp can never be placed, so it is not turned into
    # an unknown interval; it is reported as ignored instead.
    assert timeline["ignored"][0]["line_no"] == 4


def test_same_timestamp_conflict_is_unknown_and_not_ordered_by_line():
    point = _hop()
    readings = [
        _reading(1, "2025-03-01T00:00:00Z", 0, label="idle"),
        _reading(2, "2025-03-01T00:01:00Z", 2, label="run"),
        _reading(3, "2025-03-01T00:01:00Z", 1, label="warmup"),
        _reading(4, "2025-03-01T00:10:00Z", 3, label="fault"),
    ]
    timeline = _timeline(point, readings)
    # The 00:01 timestamp carries two different states, so neither is confirmed.
    assert {row.line_no for row in timeline["observations"]} == {1, 4}
    review = analyse_timeline(point, timeline)
    unknown = [item for item in review["segments"] if item["kind"] == "unknown"]
    assert len(unknown) == 1
    assert "same_time_conflict" in unknown[0]["reasons"]
    assert unknown[0]["line_numbers"] == [1, 2, 3, 2, 4] or 4 in unknown[0]["line_numbers"]


def test_duplicate_identical_payload_is_not_unknown_noise():
    point = _bounded(max_gap=900)
    readings = [
        _reading(1, "2025-03-01T00:00:00Z", 0, label="idle"),
        _reading(3, "2025-03-01T00:10:00Z", 2, label="run"),
    ]
    payload = build_response(7, 3, encode_raw(0, "uint16", "AB")).hex()
    frames = [
        _frame(1, "2025-03-01T00:00:00Z", response_hex=payload),
        _frame(3, "2025-03-01T00:10:00Z", response_hex=payload),
        _frame(2, "2025-03-01T00:00:00Z", quality="duplicate", response_hex=payload),
    ]
    timeline = _timeline(point, readings, frames)
    review = analyse_timeline(point, timeline)
    assert review["summary"]["unknown_segments"] == 0
    assert review["summary"]["observations"] == 2


def test_long_gap_merges_bad_frames_into_one_unknown_segment():
    point = _bounded(max_gap=300)
    frames = [
        _frame(2, "2025-03-01T00:03:00Z", quality="bad_crc_response",
               issue_code="bad_crc_response"),
        _frame(3, "2025-03-01T00:06:00Z", quality="exception_response",
               issue_code="exception_response"),
    ]
    readings = [
        _reading(1, "2025-03-01T00:00:00Z", 0, label="idle"),
        _reading(4, "2025-03-01T00:10:00Z", 2, label="run"),
    ]
    timeline = _timeline(point, readings, frames)
    review = analyse_timeline(point, timeline)
    unknown = [item for item in review["segments"] if item["kind"] == "unknown"]
    assert len(unknown) == 1
    assert set(unknown[0]["reasons"]) == {
        "bad_crc_response", "exception_response", "long_gap"
    }
    assert unknown[0]["start_ts"] == "2025-03-01T00:00:00Z"
    assert unknown[0]["end_ts"] == "2025-03-01T00:10:00Z"


def test_same_unknown_gap_is_one_segment_with_multiple_bad_rows():
    point = _bounded(max_gap=900)
    frames = [
        _frame(2, "2025-03-01T00:02:00Z", quality="bad_frame", issue_code="truncated_frame"),
        _frame(3, "2025-03-01T00:03:00Z", quality="bad_crc_request",
               issue_code="bad_crc_request"),
    ]
    readings = [
        _reading(1, "2025-03-01T00:00:00Z", 0, label="idle"),
        _reading(4, "2025-03-01T00:10:00Z", 2, label="run"),
    ]
    review = analyse_timeline(point, _timeline(point, readings, frames))
    unknown = [item for item in review["segments"] if item["kind"] == "unknown"]
    assert len(unknown) == 1
    assert unknown[0]["line_numbers"] == [1, 2, 3, 4]


def test_segments_carry_start_end_state_and_line_numbers():
    point = _bounded(max_gap=300)
    readings = [
        _reading(1, "2025-03-01T00:00:00Z", 0, label="idle"),
        _reading(2, "2025-03-01T00:00:30Z", 0, label="idle"),
        _reading(3, "2025-03-01T00:10:00Z", 2, label="run"),
    ]
    review = analyse_timeline(point, _timeline(point, readings))
    confirmed = [item for item in review["segments"] if item["kind"] == "confirmed"]
    assert confirmed[0]["state"]["raw_value"] == 0
    # The long gap starts at the last observation before it.
    assert confirmed[0]["start_line"] == 1 and confirmed[0]["end_line"] == 1
    assert confirmed[1]["start_line"] == 2 and confirmed[1]["end_line"] == 2
    assert confirmed[0]["start_ts"] == "2025-03-01T00:00:00Z"
    assert confirmed[0]["end_ts"] == "2025-03-01T00:00:00Z"
    assert confirmed[1]["state"]["raw_value"] == 0
    assert confirmed[2]["state"]["raw_value"] == 2
    unknown = [item for item in review["segments"] if item["kind"] == "unknown"]
    assert len(unknown) == 1
    assert unknown[0]["reasons"] == ["long_gap"]


def test_parse_ts_accepts_z_offsets_and_naive_values():
    assert parse_ts("2025-03-01T00:00:00Z") is not None
    assert parse_ts("2025-03-01T08:00:00+08:00").hour == 0
    assert parse_ts("2025-03-01T00:00:00").tzinfo is not None
    assert parse_ts("") is None and parse_ts("nope") is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_point_with_no_confirmed_observation_has_no_pairs():
    point = _hop()
    readings = [_reading(1, "2025-03-01T00:00:00Z", 9, quality="not_in_enum")]
    review = analyse_timeline(point, _timeline(point, readings))
    assert review["transitions"] == []
    assert review["summary"]["observations"] == 0
    assert review["summary"]["unknown_segments"] == 1


def test_unparseable_timestamp_is_reported_as_ignored_not_a_gap():
    point = _hop()
    readings = [
        _reading(1, "", 0),
        _reading(2, "2025-13-40T99:00:00Z", 1),
        _reading(3, "2025-03-01T00:00:00Z", 0),
    ]
    timeline = _timeline(point, readings)
    assert [item["line_no"] for item in timeline["ignored"]] == [1, 2]
    assert all(item["reason"] == "unreadable_ts" for item in timeline["ignored"])
    review = analyse_timeline(point, timeline)
    assert review["summary"]["ignored_rows"] == 2
    assert review["summary"]["unknown_segments"] == 0


def test_terminal_states_are_accepted_and_used_in_path_search():
    point = _point("""          verification: bounded
          max_gap_seconds: 900
          terminal_states: [3]
          transitions:
            0: [1]
            1: [2]
            2: [3]
""", enum="0: idle\n          1: warmup\n          2: run\n          3: halted")
    assert point.mode_state.terminal_states == (3,)
    pair = analyse_pair(point.mode_state, point.enum, 0, 3, 1000)
    assert pair["assumed_path"] == [0, 1, 2, 3]
    assert pair["grade"] == EVIDENCE_UNPROVEN
    impossible = analyse_pair(point.mode_state, point.enum, 3, 0, 1000)
    assert impossible["grade"] == EVIDENCE_IMPOSSIBLE
    assert impossible["assumed_path"] is None


def test_analysis_is_deterministic_for_repeated_calls():
    point = _bounded(max_gap=900)
    readings = [
        _reading(1, "2025-03-01T00:00:00Z", 0, label="idle"),
        _reading(2, "2025-03-01T00:10:00Z", 2, label="run"),
    ]
    frames = [_frame(3, "2025-03-01T00:12:00Z", quality="bad_frame")]
    first = analyse_timeline(point, _timeline(point, readings, frames))
    second = analyse_timeline(point, _timeline(point, readings, frames))
    assert first == second
