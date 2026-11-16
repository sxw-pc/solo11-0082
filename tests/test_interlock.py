"""Behavioural coverage of the offline two-point sustained interlock.

Each test drives a real capture end-to-end (frame decoding, quality, ledger)
and then inspects the persisted three-valued timeline and events.
"""

import pytest

from pointlog.interlock import evaluate_batch
from pointlog.pipeline import IngestInterrupted
from pointlog.store import Store
from pointlog.store_queries import (
    interlock_events,
    interlock_intervals,
    interlock_summary,
    interlock_unknown_pending,
)

from interlock_helpers import (
    TEMP_POINT,
    PRESSURE_POINT,
    bad_crc_frame,
    build_contract,
    canonical_episode,
    contradictory_frame,
    frame,
    run,
    ts,
)


def _load(db_path):
    return Store(db_path, read_only=True)


def _events(db_path, batch_id):
    store = _load(db_path)
    try:
        return interlock_events(store, batch_id)
    finally:
        store.close()


def _intervals(db_path, batch_id, state=None):
    store = _load(db_path)
    try:
        return interlock_intervals(store, batch_id, state=state)
    finally:
        store.close()


def _summary(db_path, batch_id, contract):
    store = _load(db_path)
    try:
        return interlock_summary(store, batch_id, contract)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The canonical scenario from the specification.
# ---------------------------------------------------------------------------

def test_bounded_hold_proves_the_specified_episode(tmp_path):
    contract_path, contract = build_contract(tmp_path)
    result = run(tmp_path, contract_path, canonical_episode(contract))
    assert result.status == "completed"

    events = _events(tmp_path / "ledger.db", result.batch_id)
    assert len(events) == 1
    event = events[0]
    assert event["rule_id"] == "hp"
    assert event["rule_version"] == 7
    assert event["sampling"] == "bounded_hold"
    assert event["start_ts"] == ts(2)
    assert event["end_ts"] == ts(4)
    assert event["duration_seconds"] == 120.0
    # Peak remembers the held high values, not just the boundary samples.
    assert event["peak_a"] == 70.0
    assert event["peak_b"] == 900.0
    assert event["interruption_reason"] == "explicit_normal"
    assert event["interruption_lines"] == [3]  # the 00:04 normal temperature
    # Source readings that actually contributed to the proven interval.
    assert event["line_numbers"] == [1, 2]

    summary = _summary(tmp_path / "ledger.db", result.batch_id, contract)
    rule = summary["rules"][0]
    assert rule["totals"]["event_count"] == 1
    assert rule["totals"]["proven_interlock_seconds"] == 120.0
    assert rule["totals"]["proven_normal_seconds"] == 120.0
    assert rule["totals"]["unknown_seconds"] == 120.0
    assert rule["window_start_ts"] == ts(0)
    assert rule["window_end_ts"] == ts(6)


def test_strict_simultaneous_refuses_to_claim_the_same_evidence(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, sampling="strict_simultaneous"
    )
    result = run(
        tmp_path, contract_path, canonical_episode(contract), db_name="strict.db"
    )
    events = _events(tmp_path / "strict.db", result.batch_id)
    assert events == []

    summary = _summary(tmp_path / "strict.db", result.batch_id, contract)
    rule = summary["rules"][0]
    assert rule["sampling"] == "strict_simultaneous"
    assert rule["totals"]["event_count"] == 0
    assert rule["totals"]["proven_interlock_seconds"] == 0
    assert rule["totals"]["proven_normal_seconds"] == 0
    # The whole window is explicitly unknown, not silently normal.
    assert rule["totals"]["unknown_seconds"] == 360.0
    assert rule["intervals"] and all(
        item["state"] == "unknown" for item in rule["intervals"]
    )
    assert rule["unknown_intervals"][0]["reason"] == "no_simultaneous_sample"
    assert rule["unknown_intervals"][0]["line_numbers"] == [1, 2, 3]


def test_strict_mode_uses_devices_that_do_sample_simultaneously(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, sampling="strict_simultaneous"
    )
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        frame(contract, "temp", 720, ts(2)),
        frame(contract, "pressure", 9200, ts(2)),
    ]
    result = run(tmp_path, contract_path, records, db_name="strict2.db")
    summary = _summary(tmp_path / "strict2.db", result.batch_id, contract)
    rule = summary["rules"][0]
    # Two confirmed simultaneous true instants, but zero continuous duration.
    assert rule["totals"]["proven_simultaneous_instants"] == 2
    assert rule["totals"]["proven_interlock_seconds"] == 0
    assert rule["totals"]["event_count"] == 0
    confirmed = [item for item in rule["intervals"] if item["state"] == "true"]
    assert [item["start_ts"] for item in confirmed] == [ts(0), ts(2)]
    assert all(item["duration_seconds"] == 0 for item in confirmed)


def test_both_modes_reach_their_own_authorised_conclusion(tmp_path):
    bounded_path, bounded_contract = build_contract(
        tmp_path, name="bounded.yaml"
    )
    strict_path, strict_contract = build_contract(
        tmp_path, sampling="strict_simultaneous", name="strict.yaml"
    )
    records = canonical_episode(bounded_contract)
    bounded = run(
        tmp_path, bounded_path, records, db_name="b.db", fixture_name="b.jsonl"
    )
    strict = run(
        tmp_path, strict_path, records, db_name="s.db", fixture_name="s.jsonl"
    )
    bounded_events = _events(tmp_path / "b.db", bounded.batch_id)
    strict_events = _events(tmp_path / "s.db", strict.batch_id)
    assert len(bounded_events) == 1
    assert strict_events == []
    # Same evidence, different authorised semantics, each internally coherent.
    assert bounded_events[0]["start_ts"] == ts(2)
    assert bounded_events[0]["end_ts"] == ts(4)


# ---------------------------------------------------------------------------
# Staleness, short normals, bad frames, discrete peaks.
# ---------------------------------------------------------------------------

def test_stale_limit_ends_the_event_and_opens_unknown(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, max_stale_minutes=3
    )
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        frame(contract, "pressure", 9100, ts(8)),  # only pressure refreshes
    ]
    result = run(tmp_path, contract_path, records, db_name="stale.db")
    events = _events(tmp_path / "stale.db", result.batch_id)
    assert len(events) == 1
    assert events[0]["start_ts"] == ts(0)
    assert events[0]["end_ts"] == ts(3)  # both holds reach the 3-minute limit
    assert events[0]["interruption_reason"] == "stale_evidence"
    unknown = _intervals(tmp_path / "stale.db", result.batch_id, state="unknown")
    assert unknown and unknown[0]["start_ts"] == ts(3)
    assert unknown[0]["reason"] == "stale_evidence"
    # Both sample holds expired at the same instant; both lines are traced.
    assert unknown[0]["line_numbers"] == [1, 2]


def test_never_refreshed_value_cannot_hold_forever(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, max_stale_minutes=5
    )
    result = run(
        tmp_path, contract_path,
        [frame(contract, "temp", 700, ts(0)),
         frame(contract, "pressure", 9000, ts(0)),
         frame(contract, "pressure", 9100, ts(8))],
        db_name="forever.db",
    )
    summary = _summary(tmp_path / "forever.db", result.batch_id, contract)
    totals = summary["rules"][0]["totals"]
    assert totals["proven_interlock_seconds"] == 300.0  # exactly the stale limit
    assert totals["event_count"] == 1
    events = _events(tmp_path / "forever.db", result.batch_id)
    assert events[0]["end_ts"] == ts(5)


def test_short_normal_value_cuts_the_sustained_window(tmp_path):
    contract_path, contract = build_contract(tmp_path)
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        frame(contract, "temp", 500, ts(1)),   # brief normal value
        frame(contract, "temp", 700, ts(2)),
    ]
    result = run(tmp_path, contract_path, records, db_name="cut.db")
    events = _events(tmp_path / "cut.db", result.batch_id)
    # Neither 00:00-00:01 nor 00:02-00:03 reaches the 2 minute threshold.
    assert events == []
    false_intervals = _intervals(
        tmp_path / "cut.db", result.batch_id, state="false"
    )
    assert false_intervals and false_intervals[0]["start_ts"] == ts(1)


def test_two_discrete_high_values_are_not_added_together(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, min_duration_minutes=2
    )
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        # Everything goes unknown at 00:05 (stale); a new high arrives at 00:06.
        frame(contract, "temp", 710, ts(6)),
        frame(contract, "pressure", 9100, ts(6)),
    ]
    result = run(tmp_path, contract_path, records, db_name="discrete.db")
    events = _events(tmp_path / "discrete.db", result.batch_id)
    # The 00:00-00:05 run qualifies; the isolated 00:06 instant does not.
    assert len(events) == 1
    assert events[0]["start_ts"] == ts(0)
    assert events[0]["end_ts"] == ts(5)
    summary = _summary(tmp_path / "discrete.db", result.batch_id, contract)
    # No event may span the unknown gap, so total proven time is 300 s, never 300+.
    assert summary["rules"][0]["totals"]["proven_interlock_seconds"] == 300.0


def test_bad_frame_interrupts_the_clock_and_is_traceable(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, min_duration_minutes=1, max_stale_minutes=10
    )
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        bad_crc_frame(contract, "temp", 700, ts(1)),   # bad evidence at 00:01
        frame(contract, "temp", 700, ts(4)),           # trusted again at 00:04
    ]
    result = run(tmp_path, contract_path, records, db_name="bad.db")
    events = _events(tmp_path / "bad.db", result.batch_id)
    assert len(events) == 1
    assert events[0]["end_ts"] == ts(1)
    assert events[0]["interruption_reason"] == "bad_evidence"
    assert 3 in events[0]["interruption_lines"]
    unknown = _intervals(tmp_path / "bad.db", result.batch_id, state="unknown")
    gap = next(item for item in unknown if item["start_ts"] == ts(1))
    assert gap["reason"] == "bad_evidence"
    assert 3 in gap["line_numbers"]
    # The post-gap high run is only 00:04 -> 00:05 stale, below the threshold.
    assert len(events) == 1


def test_bad_frame_between_refreshes_prevents_hold_across_it(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, min_duration_minutes=1, max_stale_minutes=10,
    )
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        bad_crc_frame(contract, "pressure", 9000, ts(2)),
        frame(contract, "temp", 700, ts(4)),
    ]
    result = run(tmp_path, contract_path, records, db_name="badhold.db")
    events = _events(tmp_path / "badhold.db", result.batch_id)
    assert len(events) == 1
    assert events[0]["end_ts"] == ts(2)
    assert events[0]["interruption_reason"] == "bad_evidence"


# ---------------------------------------------------------------------------
# Conflicts and untrustworthy rows.
# ---------------------------------------------------------------------------

def test_contradictory_values_at_one_timestamp_make_it_unknown(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, max_stale_minutes=10, min_duration_minutes=1
    )
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        frame(contract, "temp", 720, ts(2)),        # trusted high
        contradictory_frame(contract, "temp", 100, ts(2)),  # conflicting value
        frame(contract, "temp", 700, ts(4)),
    ]
    result = run(tmp_path, contract_path, records, db_name="conf.db")
    # The conflicting instant cannot be judged, so the timeline shows unknown
    # at 00:02 pointing at the offending line.
    store = _load(tmp_path / "conf.db")
    try:
        frames = store.conn.execute(
            "SELECT line_no, quality_code, issue_code FROM frames "
            "WHERE batch_id = ? ORDER BY line_no", (result.batch_id,),
        ).fetchall()
    finally:
        store.close()
    assert frames[3]["quality_code"] == "duplicate"
    assert frames[3]["issue_code"] == "contradictory_value"
    unknown = _intervals(
        tmp_path / "conf.db", result.batch_id, state="unknown"
    )
    conflict = next(item for item in unknown if item["start_ts"] == ts(2))
    assert conflict["reason"] == "conflicting_values"
    assert 3 in conflict["line_numbers"]
    events = _events(tmp_path / "conf.db", result.batch_id)
    assert all(event["end_ts"] <= ts(2) for event in events)


def test_overflow_and_under_range_readings_are_not_trusted(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, max_stale_minutes=10, min_duration_minutes=1
    )
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        frame(contract, "temp", 20000, ts(2)),  # 2000 degC > range max, overflow
        frame(contract, "temp", 700, ts(4)),
    ]
    result = run(tmp_path, contract_path, records, db_name="range.db")
    store = _load(tmp_path / "range.db")
    try:
        quality = store.conn.execute(
            "SELECT quality_code FROM readings WHERE batch_id=? AND point_key='temp' "
            "ORDER BY line_no", (result.batch_id,),
        ).fetchall()
    finally:
        store.close()
    assert [row["quality_code"] for row in quality] == ["good", "overflow", "good"]
    unknown = _intervals(tmp_path / "range.db", result.batch_id, state="unknown")
    assert any(item["start_ts"] == ts(2) for item in unknown)
    events = _events(tmp_path / "range.db", result.batch_id)
    assert all(event["end_ts"] <= ts(2) for event in events)


def test_unparseable_timestamp_is_excluded_not_treated_as_normal(tmp_path):
    contract_path, contract = build_contract(
        tmp_path, min_duration_minutes=1
    )
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        frame(contract, "temp", 500, "not-a-timestamp"),
        frame(contract, "temp", 700, ts(2)),
    ]
    result = run(tmp_path, contract_path, records, db_name="badts.db")
    events = _events(tmp_path / "badts.db", result.batch_id)
    # The bad-timestamp row contributes nothing; the trusted 00:00-00:02 run
    # still proves the interlock.
    assert len(events) == 1
    assert events[0]["start_ts"] == ts(0)
    assert events[0]["end_ts"] == ts(2)


# ---------------------------------------------------------------------------
# Ordering, duplicates, resume and isolation.
# ---------------------------------------------------------------------------

def test_out_of_order_input_yields_identical_events(tmp_path):
    contract_path, contract = build_contract(tmp_path)
    records = canonical_episode(contract)
    ordered = run(tmp_path, contract_path, records, db_name="ordered.db",
                  fixture_name="ordered.jsonl")
    shuffled = run(tmp_path, contract_path, list(reversed(records)),
                   db_name="shuffled.db", fixture_name="shuffled.jsonl")
    def conclusion(events):
        # Line numbers legitimately reflect each replay file's own ordering;
        # the event conclusion itself must be identical.
        return [
            {key: value for key, value in event.items()
             if key not in ("batch_id", "event_key", "line_numbers",
                            "interruption_lines")}
            for event in events
        ]

    left = conclusion(_events(tmp_path / "ordered.db", ordered.batch_id))
    right = conclusion(_events(tmp_path / "shuffled.db", shuffled.batch_id))
    assert left == right
    assert len(left) == 1

    # Intervals must be a well-formed, ordered partition of the observed window.
    intervals = _intervals(tmp_path / "shuffled.db", shuffled.batch_id)
    assert intervals
    for previous, current in zip(intervals, intervals[1:]):
        assert previous["end_ts"] == current["start_ts"]


def test_duplicate_replay_rows_do_not_duplicate_events(tmp_path):
    contract_path, contract = build_contract(tmp_path)
    records = canonical_episode(contract)
    repeated = records + [dict(records[0]), dict(records[1])]
    result = run(tmp_path, contract_path, repeated, db_name="dup.db")
    events = _events(tmp_path / "dup.db", result.batch_id)
    assert len(events) == 1

    store = _load(tmp_path / "dup.db")
    try:
        count = store.conn.execute(
            "SELECT COUNT(*) FROM interlock_events WHERE batch_id = ?",
            (result.batch_id,),
        ).fetchone()[0]
    finally:
        store.close()
    assert count == 1


def test_rerun_and_explicit_recompute_stay_idempotent(tmp_path):
    contract_path, contract = build_contract(tmp_path)
    first = run(tmp_path, contract_path, canonical_episode(contract), db_name="rerun.db")
    before = _events(tmp_path / "rerun.db", first.batch_id)
    second = run(tmp_path, contract_path, canonical_episode(contract), db_name="rerun.db")
    assert second.reused
    assert _events(tmp_path / "rerun.db", first.batch_id) == before

    store = Store(tmp_path / "rerun.db")
    try:
        evaluate_batch(store, first.batch_id, contract)
        evaluate_batch(store, first.batch_id, contract)
    finally:
        store.close()
    assert _events(tmp_path / "rerun.db", first.batch_id) == before


def test_resume_after_interruption_equals_a_single_run(tmp_path):
    contract_path, contract = build_contract(tmp_path)
    records = canonical_episode(contract)
    with pytest.raises(IngestInterrupted):
        run(tmp_path, contract_path, records, db_name="resume.db", stop_after=2)
    resumed = run(tmp_path, contract_path, records, db_name="resume.db", resume=True)
    assert resumed.status == "completed"

    whole = run(tmp_path, contract_path, records, db_name="whole.db",
                fixture_name="whole.jsonl")
    assert _events(tmp_path / "resume.db", resumed.batch_id) == _events(
        tmp_path / "whole.db", whole.batch_id
    )
    store = _load(tmp_path / "resume.db")
    try:
        assert store.conn.execute(
            "SELECT COUNT(*) FROM interlock_events WHERE batch_id = ?",
            (resumed.batch_id,),
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_rerun_after_partial_report_does_not_double_insert(tmp_path):
    contract_path, contract = build_contract(tmp_path)
    records = canonical_episode(contract)
    with pytest.raises(IngestInterrupted):
        run(tmp_path, contract_path, records, db_name="partial.db", stop_after=2)
    store = Store(tmp_path / "partial.db")
    try:
        batch_id = store.conn.execute("SELECT id FROM batches").fetchone()["id"]
        # Simulate an earlier evaluator run over the partial ledger.
        evaluate_batch(store, batch_id, contract)
    finally:
        store.close()
    result = run(tmp_path, contract_path, records, db_name="partial.db", resume=True)
    assert result.batch_id == batch_id
    assert len(_events(tmp_path / "partial.db", batch_id)) == 1


def test_devices_are_isolated(tmp_path):
    """A second device with the same point keys must not leak evidence."""
    temp_b = dict(TEMP_POINT, key="temp_b", start=10, slave_id=2)
    pressure_b = dict(PRESSURE_POINT, key="pressure_b", start=11, slave_id=2)
    rule_b = {
        "id": "hp_b", "version": 1,
        "points": [
            {"key": "temp_b", "threshold": 60, "direction": "above"},
            {"key": "pressure_b", "threshold": 800, "direction": "above"},
        ],
        "min_duration_minutes": 1,
        "max_stale_minutes": 5,
        "sampling": "bounded_hold",
    }
    raw = {
        "version": 1,
        "devices": [
            {"id": "plant-a", "label": "A", "points": [TEMP_POINT, PRESSURE_POINT],
             "interlocks": [{
                 "id": "hp", "version": 1,
                 "points": [
                     {"key": "temp", "threshold": 60, "direction": "above"},
                     {"key": "pressure", "threshold": 800, "direction": "above"},
                 ],
                 "min_duration_minutes": 1,
                 "max_stale_minutes": 5,
                 "sampling": "bounded_hold",
             }]},
            {"id": "plant-b", "label": "B", "points": [temp_b, pressure_b],
             "interlocks": [rule_b]},
        ],
    }
    import yaml
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    from pointlog.contract import load_contract
    contract = load_contract(contract_path)

    records = canonical_episode(contract)  # only plant-a evidence
    result = run(tmp_path, contract_path, records, db_name="iso.db")
    store = _load(tmp_path / "iso.db")
    try:
        events_a = interlock_events(store, result.batch_id, "hp")
        events_b = interlock_events(store, result.batch_id, "hp_b")
        summary = interlock_summary(store, result.batch_id, contract)
    finally:
        store.close()
    assert len(events_a) == 1
    assert events_b == []
    rule_b_summary = next(item for item in summary["rules"] if item["rule_id"] == "hp_b")
    assert rule_b_summary["totals"]["event_count"] == 0
    assert rule_b_summary["totals"]["proven_interlock_seconds"] == 0
    assert rule_b_summary["window_start_ts"] == ""  # no evidence at all


# ---------------------------------------------------------------------------
# Reporting / recollection separation.
# ---------------------------------------------------------------------------

def test_reporting_keeps_unknown_out_of_normal_and_pending(tmp_path):
    contract_path, contract = build_contract(tmp_path)
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        bad_crc_frame(contract, "temp", 700, ts(1)),
        frame(contract, "temp", 700, ts(4)),
        frame(contract, "pressure", 9000, ts(4)),
    ]
    result = run(tmp_path, contract_path, records, db_name="report.db",
                 reports_dir="reports")
    store = _load(tmp_path / "report.db")
    try:
        pending = interlock_unknown_pending(store, result.batch_id, contract)
        # Single-point statistics are untouched by the interlock layer.
        good = store.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE batch_id=? AND quality_code='good'",
            (result.batch_id,),
        ).fetchone()[0]
    finally:
        store.close()
    assert pending, "bad evidence must produce an unknown recollection entry"
    assert all(item["issue_code"] == "interlock_unknown" for item in pending)
    assert all(item["state"] == "unknown" for item in pending)
    assert all(item["line_numbers"] for item in pending)
    assert good == 4

    import json
    summary = json.loads(
        (tmp_path / "reports" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["interlock"]["rules"][0]["totals"]["unknown_seconds"] > 0
    assert summary["interlock_pending_recollect"] == [
        {**item, "line_numbers": item["line_numbers"]} for item in pending
    ]
    markdown = (tmp_path / "reports" / "summary.md").read_text(encoding="utf-8")
    assert "未知缺口（不计为正常）" in markdown
    assert "已证联锁事件" in markdown
    html = (tmp_path / "reports" / "summary.html").read_text(encoding="utf-8")
    assert "未知缺口（不计为正常）" in html
    assert "<script" not in html
    csv_text = (tmp_path / "reports" / "interlock_pending.csv").read_text(
        encoding="utf-8"
    )
    assert "interlock_unknown" in csv_text
    header = csv_text.splitlines()[0]
    assert "rule_id" in header and "unknown" not in header


# ---------------------------------------------------------------------------
# Schema compatibility and non-interference with existing statistics.
# ---------------------------------------------------------------------------

def test_schema_upgrade_adds_interlock_tables_without_losing_rows(tmp_path):
    import sqlite3

    from pointlog.schema import SCHEMA_SQL, SCHEMA_VERSION

    legacy = tmp_path / "legacy.db"
    connection = sqlite3.connect(str(legacy))
    # Build a v1-style ledger: schema minus the interlock tables.
    prefix = SCHEMA_SQL.split("-- Three-valued interlock timeline")[0]
    connection.executescript(prefix)
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', '1')"
    )
    connection.execute(
        "INSERT INTO batches(id, replay_path, replay_sha256, contract_path, "
        "contract_sha256, started_at, status) VALUES("
        "'b_old', 'r.jsonl', 'aaa', 'c.yaml', 'bbb', '2025-03-01T00:00:00Z', "
        "'completed')"
    )
    connection.commit()
    connection.close()

    store = Store(legacy)
    try:
        tables = {
            row[0] for row in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"interlock_intervals", "interlock_events"} <= tables
        assert store.conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
        assert store.get_batch("b_old") is not None
    finally:
        store.close()


def test_interlock_does_not_change_point_or_quality_statistics(tmp_path):
    from pointlog.store_queries import point_stats, quality_distribution

    contract_path, contract = build_contract(tmp_path)
    records = canonical_episode(contract)
    result = run(tmp_path, contract_path, records, db_name="stats.db")
    store = _load(tmp_path / "stats.db")
    try:
        points = point_stats(store, result.batch_id, contract)
        quality = quality_distribution(store, result.batch_id)
        readings = store.conn.execute(
            "SELECT COUNT(*) FROM readings WHERE batch_id = ?",
            (result.batch_id,),
        ).fetchone()[0]
    finally:
        store.close()
    by_key = {item["point_key"]: item for item in points}
    assert by_key["temp"]["reading_count"] == 2
    assert by_key["temp"]["good_count"] == 2
    assert by_key["pressure"]["reading_count"] == 2
    assert by_key["pressure"]["good_count"] == 2
    assert readings == 4
    assert quality == {"good": 4}


def test_missing_response_is_a_gap_not_a_bad_value(tmp_path):
    """Bounded hold continues past an absent response until the stale limit."""
    contract_path, contract = build_contract(
        tmp_path, min_duration_minutes=1, max_stale_minutes=5
    )
    missing = frame(contract, "temp", 700, ts(1))
    missing["response_hex"] = ""
    records = [
        frame(contract, "temp", 700, ts(0)),
        frame(contract, "pressure", 9000, ts(0)),
        missing,                                   # no new temperature sample
        frame(contract, "pressure", 9100, ts(4)),
        frame(contract, "pressure", 9200, ts(7)),  # window continues past expiry
    ]
    result = run(tmp_path, contract_path, records, db_name="missing.db")
    events = _events(tmp_path / "missing.db", result.batch_id)
    assert len(events) == 1
    assert events[0]["start_ts"] == ts(0)
    # The hold was not cut at 00:01; it ran until the stale limit or a sample.
    assert events[0]["end_ts"] == ts(5)
    assert events[0]["interruption_reason"] == "stale_evidence"


def test_bad_frame_cuts_immediately_where_missing_response_does_not(tmp_path):
    """Same shape, different evidence: bad CRC must interrupt, a gap must not."""
    contract_path, contract = build_contract(
        tmp_path, min_duration_minutes=1, max_stale_minutes=5
    )
    good = frame(contract, "temp", 700, ts(0))
    pressure = frame(contract, "pressure", 9000, ts(0))
    later = frame(contract, "pressure", 9100, ts(4))
    final = frame(contract, "pressure", 9200, ts(7))
    gap = frame(contract, "temp", 700, ts(1))
    gap["response_hex"] = ""
    bad = bad_crc_frame(contract, "temp", 700, ts(1))

    gap_run = run(
        tmp_path, contract_path, [good, pressure, gap, later, final],
        db_name="gap.db", fixture_name="gap.jsonl",
    )
    bad_run = run(
        tmp_path, contract_path, [good, pressure, bad, later, final],
        db_name="bad2.db", fixture_name="bad2.jsonl",
    )
    gap_event = _events(tmp_path / "gap.db", gap_run.batch_id)[0]
    bad_event = _events(tmp_path / "bad2.db", bad_run.batch_id)[0]
    assert gap_event["end_ts"] == ts(5)
    assert bad_event["end_ts"] == ts(1)
    assert bad_event["interruption_reason"] == "bad_evidence"


def test_cli_interlock_command_reports_three_valued_totals(tmp_path, capsys):
    from pointlog.cli import main

    contract_path, contract = build_contract(tmp_path)
    result = run(tmp_path, contract_path, canonical_episode(contract),
                 db_name="cli.db")
    assert main(["interlock", result.batch_id, "--db", str(tmp_path / "cli.db")]) == 0
    output = capsys.readouterr().out
    assert "双点位持续联锁" in output
    assert "已证联锁 120.0s" in output
    assert "事件 1" in output
    assert "未知" in output
    assert "运行时" not in output  # never commands anything at runtime

    assert main(["interlock", "missing-batch", "--db", str(tmp_path / "cli.db")]) == 4


def test_server_batch_page_shows_interlock_without_mutation(tmp_path):
    """The loopback review console exposes the interlock section read-only."""
    import http.client
    import threading

    from pointlog.server import create_server

    contract_path, contract = build_contract(tmp_path)
    result = run(tmp_path, contract_path, canonical_episode(contract),
                 db_name="srv.db")
    server = create_server(tmp_path / "srv.db", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", f"/batch/{result.batch_id}")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert response.status == 200
    assert "双点位持续联锁" in body
    assert "已证联锁事件" in body
    assert "未知缺口（不计为正常）" in body
    assert "<script" not in body
