"""Split-frame (separate request/response event) replay pairing tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from pointlog.codec import encode_raw
from pointlog.contract import load_contract
from pointlog.modbus import (
    build_exception_response,
    build_request,
    build_response,
    corrupt_crc,
)
from pointlog.pipeline import IngestInterrupted, run_capture
from pointlog.report_stats import collect_summary
from pointlog.store import Store
from pointlog.util import sha256_text

BASE_TIME = datetime(2025, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

POINT_LIBRARY = {
    "speed": {
        "key": "speed", "slave_id": 1, "function": 3, "start": 0, "count": 2,
        "data_type": "uint32", "byte_order": "ABCD", "scale": 0.1, "offset": 0,
        "unit": "rpm", "range": [0, 3000],
    },
    "temperature": {
        "key": "temperature", "slave_id": 1, "function": 3, "start": 2, "count": 1,
        "data_type": "int16", "byte_order": "AB", "scale": 0.1, "offset": 0,
        "unit": "degC", "range": [-40, 150],
    },
    "flow": {
        "key": "flow", "slave_id": 1, "function": 4, "start": 10, "count": 2,
        "data_type": "float32", "byte_order": "CDAB", "scale": 1, "offset": 0,
        "unit": "m3/h", "range": [0, 500],
    },
    "pressure": {
        "key": "pressure", "slave_id": 2, "function": 3, "start": 30, "count": 1,
        "data_type": "uint16", "byte_order": "AB", "scale": 1, "offset": 0,
        "unit": "kPa", "range": [0, 1000],
    },
}


def _ts(seconds):
    return (BASE_TIME + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _write_contract(path, *point_keys):
    doc = {
        "version": 1,
        "devices": [{
            "id": "dev-a",
            "label": "Device A",
            "points": [POINT_LIBRARY[key] for key in point_keys],
        }],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _req(contract, key):
    point = contract.get(key)
    return build_request(point.slave_id, point.function, point.start, point.count).hex()


def _resp(contract, key, raw):
    point = contract.get(key)
    payload = encode_raw(raw, point.data_type, point.byte_order)
    return build_response(point.slave_id, point.function, payload).hex()


def _exc(contract, key, code=2):
    point = contract.get(key)
    return build_exception_response(point.slave_id, point.function, code).hex()


def _ev(ts, channel, direction, frame_hex, pair_id=None, timeout_ms=None):
    event = {
        "ts": _ts(ts),
        "channel": channel,
        "direction": direction,
        "frame_hex": frame_hex,
    }
    if pair_id is not None:
        event["pair_id"] = pair_id
    if timeout_ms is not None:
        event["timeout_ms"] = timeout_ms
    return event


def _write_events(path, events):
    lines = [json.dumps(event, ensure_ascii=False, separators=(",", ":"))
             for event in events]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return lines


def _run_events(replay, contract_path, db, *, route="explicit", timeout_ms=1000,
                reports=None, resume=False, stop_after=None):
    return run_capture(
        replay_path=replay,
        contract_path=contract_path,
        db_path=db,
        reports_dir=reports,
        replay_mode="events",
        pairing_route=route,
        pair_timeout_ms=timeout_ms,
        resume=resume,
        stop_after=stop_after,
    )


def _table(db, table):
    store = Store(db, read_only=True)
    try:
        return [dict(row) for row in store.conn.execute(f"SELECT * FROM {table}")]
    finally:
        store.close()


def _by_line(db, table):
    return {row["line_no"]: row for row in _table(db, table)}


def _issues(db):
    return _table(db, "issues")


def _dump_ledger(db):
    """Content-only ledger snapshot for cross-run consistency comparison."""
    snapshot = {}
    snapshot["frames"] = sorted(
        _table(db, "frames"), key=lambda row: row["line_no"]
    )
    snapshot["pair_events"] = sorted(
        _table(db, "pair_events"), key=lambda row: row["line_no"]
    )
    readings = _table(db, "readings")
    for row in readings:
        row.pop("id", None)
    snapshot["readings"] = sorted(
        readings, key=lambda row: (row["device_id"], row["point_key"], row["ts"])
    )
    issues = _table(db, "issues")
    for row in issues:
        row.pop("id", None)
    snapshot["issues"] = sorted(
        issues,
        key=lambda row: (row["line_no"], row["issue_code"], row["point_key"],
                         row["detail"]),
    )
    return snapshot


# ---------------------------------------------------------------------------
# 1. Explicit-evidence route: matching pair identifiers produce readings
# ---------------------------------------------------------------------------

def test_explicit_pairing_produces_readings_with_event_evidence(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed", "temperature")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    raw_lines = _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed"), pair_id="p1"),
        _ev(0.3, "COM1", "response", _resp(contract, "speed", 12340), pair_id="p1"),
        _ev(1.0, "COM1", "request", _req(contract, "temperature"), pair_id="p2"),
        _ev(1.2, "COM1", "response", _resp(contract, "temperature", 253), pair_id="p2"),
        _ev(2.0, "COM1", "request", _req(contract, "speed"), pair_id="p3"),
        _ev(2.2, "COM1", "response",
            corrupt_crc(bytes.fromhex(_resp(contract, "speed", 12340))).hex(),
            pair_id="p3"),
    ])
    db = tmp_path / "pointlog.db"
    reports = tmp_path / "reports"
    result = _run_events(replay, contract_path, db, route="explicit", reports=reports)

    assert result.status == "completed"
    assert result.replay_mode == "events"
    assert result.pairing_route == "explicit"
    assert result.reading_count == 2
    assert result.valid_readings == 2
    assert result.issue_count == 1  # the corrupted-CRC pair only

    frames = _by_line(db, "frames")
    good = frames[2]
    assert good["quality_code"] == "good"
    assert good["channel"] == "COM1"
    assert good["pair_id"] == "p1"
    assert good["pair_route"] == "explicit"
    assert good["request_line_no"] == 1
    assert good["response_line_no"] == 2
    assert good["request_sha256"] == sha256_text(raw_lines[0])
    assert good["response_sha256"] == sha256_text(raw_lines[1])
    # Existing quality priority is reused for paired frames.
    assert frames[6]["quality_code"] == "bad_crc_response"
    assert frames[6]["request_line_no"] == 5
    assert frames[6]["response_line_no"] == 6

    events = _by_line(db, "pair_events")
    assert events[1]["outcome"] == "paired" and events[1]["paired_line_no"] == 2
    assert events[2]["outcome"] == "paired" and events[2]["paired_line_no"] == 1
    assert events[1]["event_sha256"] == sha256_text(raw_lines[0])

    readings = _table(db, "readings")
    assert {row["point_key"] for row in readings} == {"speed", "temperature"}
    speed = next(row for row in readings if row["point_key"] == "speed")
    assert speed["engineering_value"] == 1234.0
    assert speed["ts"] == _ts(0.3)  # response event timestamp
    assert speed["line_no"] == 2

    store = Store(db, read_only=True)
    try:
        summary = collect_summary(store, result.batch_id, contract)
    finally:
        store.close()
    assert summary["batch"]["replay_mode"] == "events"
    assert summary["batch"]["pairing_route"] == "explicit"
    assert summary["batch"]["pair_timeout_ms"] == 1000
    assert summary["pairing"]["events"] == 6
    assert summary["pairing"]["outcomes"] == {"paired": 6}
    assert summary["totals"]["readings"] == 2
    assert summary["totals"]["pending_points"] == 1
    assert summary["quality_code_distribution"] == {"good": 2, "bad_crc_response": 1}
    on_disk = json.loads((reports / "summary.json").read_text(encoding="utf-8"))
    assert on_disk["pairing"] == summary["pairing"]


# ---------------------------------------------------------------------------
# 2. Same recording, two routes: explicit stays pending, single-in-flight reads
# ---------------------------------------------------------------------------

def test_routes_disagree_only_by_authorisation(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed")),
        _ev(0.3, "COM1", "response", _resp(contract, "speed", 12340)),
    ])
    db = tmp_path / "pointlog.db"

    explicit = _run_events(replay, contract_path, db, route="explicit")
    assert explicit.reading_count == 0
    assert explicit.issue_count == 2
    explicit_issues = _issues(db)
    assert {row["issue_code"] for row in explicit_issues} == {"unpaired_event"}
    assert all(row["pending_collect"] == 1 for row in explicit_issues)

    store = Store(db, read_only=True)
    try:
        explicit_summary = collect_summary(store, explicit.batch_id, contract)
    finally:
        store.close()
    # The pending response is not an independent sample.
    assert explicit_summary["totals"]["readings"] == 0
    assert explicit_summary["pairing"]["outcomes"] == {"unpaired": 2}
    assert explicit_summary["quality_code_distribution"] == {"unpaired_event": 2}

    inferred = _run_events(replay, contract_path, db, route="single_in_flight")
    assert inferred.batch_id != explicit.batch_id  # route is part of the fingerprint
    assert inferred.reading_count == 1
    assert inferred.valid_readings == 1
    assert inferred.issue_count == 0

    frames = _by_line(db, "frames")
    pair_frame = next(
        row for row in frames.values()
        if row["pair_route"] == "single_in_flight"
    )
    assert pair_frame["quality_code"] == "good"
    assert pair_frame["request_line_no"] == 1
    assert pair_frame["response_line_no"] == 2

    store = Store(db, read_only=True)
    try:
        inferred_summary = collect_summary(store, inferred.batch_id, contract)
        batches = store.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    finally:
        store.close()
    assert inferred_summary["totals"]["readings"] == 1
    assert inferred_summary["pairing"]["outcomes"] == {"paired": 2}
    assert batches == 2


# ---------------------------------------------------------------------------
# 3. Cross-channel pairing is refused even with a matching pair identifier
# ---------------------------------------------------------------------------

def test_cross_channel_pairing_is_refused(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed"), pair_id="p1"),
        _ev(0.3, "COM2", "response", _resp(contract, "speed", 12340), pair_id="p1"),
    ])
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route="explicit")

    assert result.reading_count == 0
    frames = _by_line(db, "frames")
    assert frames[1]["quality_code"] == "pair_timeout"  # request died on COM1
    assert frames[1]["channel"] == "COM1"
    assert frames[2]["quality_code"] == "unpaired_event"  # orphan response on COM2
    assert frames[2]["channel"] == "COM2"
    # No frame may join the two events across channels.
    assert not any(
        row["request_line_no"] == 1 and row["response_line_no"] == 2
        for row in frames.values()
    )
    issues = _issues(db)
    assert {row["issue_code"] for row in issues} == {"pair_incomplete", "unpaired_event"}
    # Same identifier on two channels is not a duplicate-pair-id violation.
    assert "duplicate_pair_id" not in {row["issue_code"] for row in issues}
    events = _by_line(db, "pair_events")
    assert events[1]["outcome"] == "timeout"
    assert events[2]["outcome"] == "unpaired"


# ---------------------------------------------------------------------------
# 4. Duplicate explicit identifiers and inverted order leave problem evidence
# ---------------------------------------------------------------------------

def test_duplicate_pair_id_points_at_both_requests(tmp_path):
    contract_path = _write_contract(
        tmp_path / "contract.yaml", "speed", "temperature"
    )
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed"), pair_id="p1"),
        _ev(0.5, "COM1", "request", _req(contract, "temperature"), pair_id="p1"),
        _ev(0.8, "COM1", "response", _resp(contract, "speed", 12340), pair_id="p1"),
    ])
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route="explicit")

    assert result.reading_count == 1  # only the first request may pair
    frames = _by_line(db, "frames")
    assert frames[2]["quality_code"] == "duplicate_pair_id"
    assert "line 1" in frames[2]["detail"]
    assert "different request frame" in frames[2]["detail"]
    assert frames[3]["quality_code"] == "good"
    assert frames[3]["request_line_no"] == 1
    events = _by_line(db, "pair_events")
    assert events[2]["outcome"] == "duplicate_pair_id"
    assert events[1]["outcome"] == "paired"
    issues = _issues(db)
    duplicate = next(row for row in issues if row["issue_code"] == "duplicate_pair_id")
    assert duplicate["point_key"] == "temperature"
    assert duplicate["pending_collect"] == 1


def test_response_earlier_than_request_is_rejected(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(1.0, "COM1", "request", _req(contract, "speed"), pair_id="p1"),
        _ev(0.5, "COM1", "response", _resp(contract, "speed", 12340), pair_id="p1"),
    ])
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route="explicit")

    assert result.reading_count == 0
    frames = _by_line(db, "frames")
    assert frames[2]["quality_code"] == "pair_order_inverted"
    assert "predates" in frames[2]["detail"]
    events = _by_line(db, "pair_events")
    assert events[2]["outcome"] == "rejected"
    assert events[1]["outcome"] == "timeout"  # request never paired afterwards
    issues = _issues(db)
    assert {row["issue_code"] for row in issues} == {
        "pair_order_inverted", "pair_incomplete",
    }


def test_explicit_response_after_timeout_leaves_evidence(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed"),
            pair_id="p1", timeout_ms=500),
        _ev(1.5, "COM1", "response", _resp(contract, "speed", 12340), pair_id="p1"),
    ])
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route="explicit")

    assert result.reading_count == 0
    frames = _by_line(db, "frames")
    assert frames[1]["quality_code"] == "pair_timeout"  # request expired at 0.5s
    assert frames[2]["quality_code"] == "pair_timeout"  # late response is evidence
    assert "declared timeout" in frames[2]["detail"]
    events = _by_line(db, "pair_events")
    assert events[1]["outcome"] == "timeout"
    assert events[2]["outcome"] == "late"
    issues = _issues(db)
    assert all(row["issue_code"] == "pair_timeout" for row in issues)
    assert all(row["pending_collect"] == 1 for row in issues)


# ---------------------------------------------------------------------------
# 5. Late-response ambiguity after timeout: no guessing, evidence only
# ---------------------------------------------------------------------------

def test_timeout_late_response_ambiguity_is_not_guessed(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed")),   # expires at 1.0s
        _ev(1.5, "COM1", "request", _req(contract, "speed")),   # new request
        _ev(1.8, "COM1", "response", _resp(contract, "speed", 12340)),
    ])
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route="single_in_flight")

    # Neither most-recent nor FIFO attribution is allowed: no reading at all.
    assert result.reading_count == 0
    assert _table(db, "readings") == []

    frames = _by_line(db, "frames")
    assert frames[1]["quality_code"] == "pair_timeout"      # first request expired
    assert frames[2]["quality_code"] == "pair_timeout"      # second never resolved
    assert frames[3]["quality_code"] == "pair_ambiguous"
    detail = frames[3]["detail"]
    assert "line 1" in detail and "line 2" in detail        # both candidates named
    assert "sha256" in detail                                # candidate hashes recorded
    assert "guessing" in detail

    issues = _issues(db)
    assert {row["issue_code"] for row in issues} == {
        "pair_timeout", "pair_ambiguous", "pair_incomplete",
    }
    assert all(row["pending_collect"] == 1 for row in issues)
    events = _by_line(db, "pair_events")
    assert events[1]["outcome"] == "timeout"
    assert events[2]["outcome"] == "timeout"
    assert events[3]["outcome"] == "ambiguous"

    store = Store(db, read_only=True)
    try:
        summary = collect_summary(store, result.batch_id, contract)
    finally:
        store.close()
    assert summary["totals"]["readings"] == 0
    assert summary["totals"]["pending_points"] == 1
    assert summary["quality_code_distribution"] == {
        "pair_timeout": 2, "pair_ambiguous": 1,
    }


def test_late_response_without_competition_is_evidence_not_a_sample(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed")),   # expires at 1.0s
        _ev(1.5, "COM1", "response", _resp(contract, "speed", 12340)),
    ])
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route="single_in_flight")

    assert result.reading_count == 0
    frames = _by_line(db, "frames")
    assert frames[1]["quality_code"] == "pair_timeout"
    assert frames[2]["quality_code"] == "pair_timeout"
    assert frames[2]["issue_code"] == "late_response"
    assert "line 1" in frames[2]["detail"]
    events = _by_line(db, "pair_events")
    assert events[1]["outcome"] == "timeout"
    assert events[2]["outcome"] == "late"


def test_two_concurrent_open_candidates_are_never_inferred(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed"), timeout_ms=10000),
        _ev(1.0, "COM1", "request", _req(contract, "speed"), timeout_ms=10000),
        _ev(2.0, "COM1", "response", _resp(contract, "speed", 12340)),
    ])
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route="single_in_flight")

    assert result.reading_count == 0
    assert _table(db, "readings") == []
    frames = _by_line(db, "frames")
    assert frames[3]["quality_code"] == "pair_ambiguous"
    assert "line 1" in frames[3]["detail"] and "line 2" in frames[3]["detail"]
    # Both requests remain unresolved evidence at end of recording.
    assert frames[1]["issue_code"] == "pair_incomplete"
    assert frames[2]["issue_code"] == "pair_incomplete"
    events = _by_line(db, "pair_events")
    assert events[3]["outcome"] == "ambiguous"


# ---------------------------------------------------------------------------
# 6. Exception responses associate with the request but never become readings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", ["explicit", "single_in_flight"])
def test_exception_response_is_associated_but_not_a_reading(tmp_path, route):
    contract_path = _write_contract(tmp_path / "contract.yaml", "flow")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    pair_id = "p1" if route == "explicit" else None
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "flow"), pair_id=pair_id),
        _ev(0.2, "COM1", "response", _exc(contract, "flow", 2), pair_id=pair_id),
    ])
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route=route)

    assert result.reading_count == 0
    assert result.valid_readings == 0
    frames = _by_line(db, "frames")
    frame = frames[2]
    assert frame["quality_code"] == "exception_response"
    assert frame["issue_code"] == "exception_response"
    assert "illegal data address" in frame["detail"]
    # The exception is associated with its request like a paired-mode line.
    assert frame["request_line_no"] == 1
    assert frame["response_line_no"] == 2
    assert frame["request_sha256"]
    events = _by_line(db, "pair_events")
    assert events[1]["outcome"] == "paired"
    assert events[2]["outcome"] == "paired"
    issues = _issues(db)
    assert len(issues) == 1
    assert issues[0]["issue_code"] == "exception_response"
    assert issues[0]["pending_collect"] == 1
    assert issues[0]["point_key"] == "flow"


# ---------------------------------------------------------------------------
# 7. Interrupted runs resume with identical pairing identity and evidence
# ---------------------------------------------------------------------------

def _resume_scenario_events(contract):
    return [
        _ev(0.0, "COM1", "request", _req(contract, "speed")),
        _ev(0.2, "COM1", "response", _resp(contract, "speed", 12340)),
        _ev(1.0, "COM1", "request", _req(contract, "flow")),      # expires at 2.0s
        _ev(1.5, "COM1", "request", _req(contract, "temperature")),
        # -- interruption happens after line 4; flow is still open here --
        _ev(1.8, "COM1", "response", _resp(contract, "temperature", 253)),
        _ev(2.5, "COM1", "request", _req(contract, "speed")),     # expires flow
        _ev(2.7, "COM1", "response", _resp(contract, "speed", 12000)),
        _ev(4.0, "COM1", "request", _req(contract, "temperature")),  # never answered
    ]


def test_resume_keeps_pairing_identity_and_evidence(tmp_path):
    contract_path = _write_contract(
        tmp_path / "contract.yaml", "speed", "temperature", "flow"
    )
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, _resume_scenario_events(contract))

    resumed_db = tmp_path / "resumed.db"
    with pytest.raises(IngestInterrupted) as excinfo:
        _run_events(replay, contract_path, resumed_db,
                    route="single_in_flight", stop_after=4)
    batch_id = excinfo.value.batch_id

    store = Store(resumed_db, read_only=True)
    try:
        batch = store.get_batch(batch_id)
        assert batch["status"] == "partial"
        assert batch["processed_lines"] == 4
        state = store.get_resume_state(
            str(replay.resolve()), batch["replay_sha256"]
        )
        assert state["line_offset"] == 4
    finally:
        store.close()

    result = _run_events(replay, contract_path, resumed_db,
                         route="single_in_flight", resume=True)
    assert result.batch_id == batch_id
    assert result.resumed
    assert result.status == "completed"
    assert result.processed_lines == 8
    assert result.reading_count == 3
    assert result.valid_readings == 3
    assert result.issue_count == 2  # flow timeout + trailing unanswered request

    frames = _by_line(resumed_db, "frames")
    # The request that timed out across the resume boundary is recorded once,
    # as timed out — not re-tracked as a new request after the interruption.
    assert frames[3]["quality_code"] == "pair_timeout"
    assert frames[3]["issue_code"] == "pair_timeout"
    assert frames[8]["issue_code"] == "pair_incomplete"
    assert frames[5]["quality_code"] == "good"
    assert frames[5]["request_line_no"] == 4
    assert frames[7]["request_line_no"] == 6
    events = _by_line(resumed_db, "pair_events")
    assert events[3]["outcome"] == "timeout"
    assert events[8]["outcome"] == "timeout"
    assert [row["outcome"] for row in
            sorted(_table(resumed_db, "pair_events"),
                   key=lambda row: row["line_no"])] == [
        "paired", "paired", "timeout", "paired", "paired", "paired", "paired",
        "timeout",
    ]

    # An uninterrupted run of the same file must produce the identical ledger.
    fresh_db = tmp_path / "fresh.db"
    fresh = _run_events(replay, contract_path, fresh_db, route="single_in_flight")
    assert fresh.batch_id == batch_id
    assert _dump_ledger(resumed_db) == _dump_ledger(fresh_db)

    # Re-running the completed batch reuses it without changing anything.
    before = _dump_ledger(resumed_db)
    again = _run_events(replay, contract_path, resumed_db, route="single_in_flight")
    assert again.reused
    assert again.reading_count == 3
    assert _dump_ledger(resumed_db) == before

    store = Store(resumed_db, read_only=True)
    try:
        summary = collect_summary(store, batch_id, contract)
        state = store.get_resume_state(
            str(replay.resolve()), store.get_batch(batch_id)["replay_sha256"]
        )
    finally:
        store.close()
    assert state["line_offset"] == 8
    assert summary["totals"]["readings"] == 3
    assert summary["totals"]["pending_points"] == 2  # flow + temperature
    assert summary["pairing"]["events"] == 8


def test_resume_after_resume_state_is_deleted_keeps_pairing(tmp_path):
    contract_path = _write_contract(
        tmp_path / "contract.yaml", "speed", "temperature", "flow"
    )
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, _resume_scenario_events(contract))
    db = tmp_path / "pointlog.db"

    with pytest.raises(IngestInterrupted):
        _run_events(replay, contract_path, db, route="single_in_flight",
                    stop_after=4)
    store = Store(db)
    try:
        store.conn.execute("DELETE FROM resume_state")
        store.conn.commit()
    finally:
        store.close()

    result = _run_events(replay, contract_path, db, route="single_in_flight",
                         resume=True)
    assert result.status == "completed"
    assert result.reading_count == 3

    fresh_db = tmp_path / "fresh.db"
    _run_events(replay, contract_path, fresh_db, route="single_in_flight")
    assert _dump_ledger(db) == _dump_ledger(fresh_db)


def test_resume_rejects_a_different_pairing_route(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    contract = load_contract(contract_path)
    replay = tmp_path / "events.jsonl"
    _write_events(replay, [
        _ev(0.0, "COM1", "request", _req(contract, "speed")),
        _ev(0.3, "COM1", "response", _resp(contract, "speed", 12340)),
    ])
    db = tmp_path / "pointlog.db"
    with pytest.raises(IngestInterrupted):
        _run_events(replay, contract_path, db, route="explicit", stop_after=1)

    from pointlog.resume import ResumeRejected

    with pytest.raises(ResumeRejected) as excinfo:
        _run_events(replay, contract_path, db, route="single_in_flight", resume=True)
    assert excinfo.value.code == "pairing_changed"


# ---------------------------------------------------------------------------
# Malformed events leave frame-level evidence, like paired-mode invalid lines
# ---------------------------------------------------------------------------

def test_malformed_events_are_bad_frame_evidence(tmp_path):
    contract_path = _write_contract(tmp_path / "contract.yaml", "speed")
    replay = tmp_path / "events.jsonl"
    replay.write_text(
        '{"ts": "broken"\n'
        '{"ts": "2025-03-01T00:00:00Z", "channel": "COM1", '
        '"direction": "sideways", "frame_hex": "010300000002c40b"}\n',
        encoding="utf-8",
    )
    db = tmp_path / "pointlog.db"
    result = _run_events(replay, contract_path, db, route="explicit")

    assert result.status == "completed"
    assert result.reading_count == 0
    frames = _by_line(db, "frames")
    assert frames[1]["quality_code"] == "bad_frame"
    assert frames[1]["issue_code"] == "invalid_json"
    assert frames[2]["quality_code"] == "bad_frame"
    assert frames[2]["issue_code"] == "invalid_line"
    events = _by_line(db, "pair_events")
    assert events[1]["outcome"] == "invalid"
    assert events[2]["outcome"] == "invalid"
    # The contract point was never seen: the usual missing-point evidence.
    assert any(row["issue_code"] == "missing_response" and row["line_no"] == 0
               for row in _issues(db))
