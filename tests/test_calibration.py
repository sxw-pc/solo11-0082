"""Synthetic coverage for calibration version tables, recalculation and views."""

import json
from pathlib import Path

import pytest

from pointlog.codec import encode_raw
from pointlog.contract import ContractError, parse_contract
from pointlog.modbus import build_request, build_response, corrupt_crc
from pointlog.pipeline import IngestInterrupted, run_capture
from pointlog.recalibrate import RecalibrationInterrupted, run_recalibration
from pointlog.report_stats import collect_summary
from pointlog.store import Store

FIXED_NOW = "2025-03-02T00:00:00Z"


def _now():
    return FIXED_NOW


def _contract(calibrations="", *, range_bounds=(0, 100), report="", extra_points=""):
    text = f"""\
version: 1
{report}devices:
  - id: dev-1
    label: Synthetic calibration device
    points:
      - key: p_cal
        slave_id: 1
        function: 3
        start: 0
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        unit: rpm
        range: [{range_bounds[0]}, {range_bounds[1]}]
"""
    if calibrations:
        text += "        calibrations:\n" + calibrations
    if extra_points:
        text += extra_points
    return text


STATIC_POINT = """\
      - key: p_static
        slave_id: 1
        function: 3
        start: 5
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 1
        offset: 0
        unit: ""
        range: [0, 100]
"""

REV_V1 = """\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-v1
                scale: 1
                offset: 0
                effective:
                  - ["2025-03-01T00:00:00Z", null]
"""

REV_V2 = """\
          - registered_at: "2025-03-01T12:00:00Z"
            versions:
              - version_id: cal-v2
                scale: 2
                offset: 0
                effective:
                  - ["2025-03-01T09:00:00Z", null]
"""

REV_V3 = """\
          - registered_at: "2025-03-01T13:00:00Z"
            versions:
              - version_id: cal-v3
                scale: 3
                offset: 0
                effective:
                  - ["2025-03-01T09:00:00Z", null]
"""


def _record(ts, raw, *, start=0, corrupt=False):
    request = build_request(1, 3, start, 1)
    response = build_response(1, 3, encode_raw(raw, "uint16", "AB"))
    if corrupt:
        response = corrupt_crc(response)
    return {
        "ts": ts,
        "request_hex": request.hex(),
        "response_hex": response.hex(),
        "source": "cal-test",
    }


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _replay(path: Path, records) -> Path:
    text = "".join(
        json.dumps(record, separators=(",", ":")) + "\n" for record in records
    )
    return _write(path, text)


def _run(paths, contract_path, **kwargs):
    kwargs.setdefault("now_factory", _now)
    return run_capture(
        replay_path=paths["replay"],
        contract_path=contract_path,
        db_path=paths["db"],
        reports_dir=paths.get("reports"),
        **kwargs,
    )


def _recalc(paths, contract_path, **kwargs):
    kwargs.setdefault("now_factory", _now)
    return run_recalibration(
        db_path=paths["db"],
        contract_path=contract_path,
        reports_dir=paths.get("reports"),
        **kwargs,
    )


def _make_paths(tmp_path):
    return {
        "replay": tmp_path / "replay.jsonl",
        "db": tmp_path / "pointlog.db",
        "reports": tmp_path / "reports",
    }


def _table(db_path, table):
    store = Store(db_path, read_only=True)
    try:
        rows = store.conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(row) for row in rows]
    finally:
        store.close()


def _summary(db_path, batch_id, contract_path):
    store = Store(db_path, read_only=True)
    try:
        from pointlog.contract import load_contract

        return collect_summary(store, batch_id, load_contract(contract_path))
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Interval boundaries, gaps and overlap validation
# ---------------------------------------------------------------------------

BOUNDARY_REVISION = """\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                scale: 1
                offset: 0
                effective:
                  - ["2025-03-01T09:00:00Z", "2025-03-01T10:00:00Z"]
              - version_id: cal-b
                scale: 10
                offset: 0
                effective:
                  - ["2025-03-01T10:00:00Z", "2025-03-01T11:00:00Z"]
"""


def test_interval_boundaries_are_half_open(tmp_path):
    paths = _make_paths(tmp_path)
    contract = _write(
        tmp_path / "points.yaml", _contract(BOUNDARY_REVISION)
    )
    timestamps = [
        ("2025-03-01T08:59:59Z", None, "calibration_unknown", ""),
        ("2025-03-01T09:00:00Z", 1.0, "good", "cal-a"),
        ("2025-03-01T09:59:59Z", 1.0, "good", "cal-a"),
        ("2025-03-01T10:00:00Z", 10.0, "good", "cal-b"),
        ("2025-03-01T10:59:59Z", 10.0, "good", "cal-b"),
        ("2025-03-01T11:00:00Z", None, "calibration_unknown", ""),
    ]
    _replay(paths["replay"], [_record(ts, 1) for ts, *_ in timestamps])
    result = _run(paths, contract)
    assert result.status == "completed"

    readings = _table(paths["db"], "readings")
    assert len(readings) == len(timestamps)
    for reading, (ts, engineering, quality, version_id) in zip(readings, timestamps):
        assert reading["ts"] == ts
        assert reading["raw_value"] == 1.0
        assert reading["engineering_value"] == engineering
        assert reading["quality_code"] == quality
        assert reading["calibration_version_id"] == version_id
        if version_id:
            assert reading["calibration_registered_at"] == "2025-03-01T08:00:00Z"
        else:
            assert reading["calibration_registered_at"] == ""

    # Gaps are undeterminable and enter the pending-recollect list instead of
    # being filled from neighbouring versions.
    unknown = [row for row in readings if row["quality_code"] == "calibration_unknown"]
    assert len(unknown) == 2
    issues = _table(paths["db"], "issues")
    gap_issues = [row for row in issues if row["issue_code"] == "calibration_unknown"]
    assert len(gap_issues) == 2
    assert all(row["pending_collect"] == 1 for row in gap_issues)
    assert all("never filled" in row["detail"] or "undeterminable" in row["detail"]
               for row in gap_issues)


def test_unparseable_timestamp_is_undeterminable(tmp_path):
    paths = _make_paths(tmp_path)
    contract = _write(tmp_path / "points.yaml", _contract(REV_V1))
    _replay(paths["replay"], [_record("not-a-timestamp", 7)])
    result = _run(paths, contract)
    assert result.reading_count == 1
    reading = _table(paths["db"], "readings")[0]
    assert reading["engineering_value"] is None
    assert reading["raw_value"] == 7.0
    assert reading["quality_code"] == "calibration_unknown"
    issue = _table(paths["db"], "issues")[0]
    assert "not a parseable UTC timestamp" in issue["detail"]


def test_overlap_inside_one_revision_is_contract_error(tmp_path):
    overlapping = """\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                scale: 1
                offset: 0
                effective:
                  - ["2025-03-01T09:00:00Z", "2025-03-01T10:00:00Z"]
              - version_id: cal-b
                scale: 2
                offset: 0
                effective:
                  - ["2025-03-01T09:30:00Z", "2025-03-01T11:00:00Z"]
"""
    with pytest.raises(ContractError) as excinfo:
        parse_contract(_contract(overlapping), source="overlap.yaml")
    assert excinfo.value.code == "contract_calibration_overlap"

    # The same overlap across two different revisions is legal: the later
    # registration replaces the earlier one.
    replacing = """\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                scale: 1
                offset: 0
                effective:
                  - ["2025-03-01T09:00:00Z", "2025-03-01T10:00:00Z"]
          - registered_at: "2025-03-01T09:00:00Z"
            versions:
              - version_id: cal-b
                scale: 2
                offset: 0
                effective:
                  - ["2025-03-01T09:30:00Z", "2025-03-01T11:00:00Z"]
"""
    contract = parse_contract(_contract(replacing), source="replace.yaml")
    assert [rev.registered_at.hour for rev in contract.get("p_cal").calibrations] == [8, 9]


def test_open_ended_interval_blocks_later_intervals():
    overlapping = """\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                scale: 1
                offset: 0
                effective:
                  - ["2025-03-01T09:00:00Z", null]
              - version_id: cal-b
                scale: 2
                offset: 0
                effective:
                  - ["2025-03-01T12:00:00Z", "2025-03-01T13:00:00Z"]
"""
    with pytest.raises(ContractError) as excinfo:
        parse_contract(_contract(overlapping), source="open.yaml")
    assert excinfo.value.code == "contract_calibration_overlap"


@pytest.mark.parametrize(
    ("block", "code"),
    [
        # registered_at must be unique per point
        ("""\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                effective:
                  - ["2025-03-01T09:00:00Z", null]
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-b
                effective:
                  - ["2025-03-01T09:00:00Z", null]
""", "contract_calibration"),
        # version ids must be unique across revisions of one point
        ("""\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                effective:
                  - ["2025-03-01T09:00:00Z", null]
          - registered_at: "2025-03-01T09:00:00Z"
            versions:
              - version_id: cal-a
                effective:
                  - ["2025-03-01T09:00:00Z", null]
""", "contract_calibration"),
        # interval start must be before end
        ("""\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                effective:
                  - ["2025-03-01T10:00:00Z", "2025-03-01T09:00:00Z"]
""", "contract_calibration"),
        # registered_at must be an aware UTC timestamp
        ("""\
          - registered_at: "not-a-time"
            versions:
              - version_id: cal-a
                effective:
                  - ["2025-03-01T09:00:00Z", null]
""", "contract_calibration"),
        # naive interval bounds (no offset) are rejected
        ("""\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                effective:
                  - ["2025-03-01T09:00:00", null]
""", "contract_calibration"),
        # calibration scale follows the same non-zero rule
        ("""\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                scale: 0
                effective:
                  - ["2025-03-01T09:00:00Z", null]
""", "contract_scale"),
    ],
)
def test_calibration_validation_errors(block, code):
    with pytest.raises(ContractError) as excinfo:
        parse_contract(_contract(block), source="bad.yaml")
    assert excinfo.value.code == code


def test_unknown_report_view_is_contract_error():
    with pytest.raises(ContractError) as excinfo:
        parse_contract(
            _contract(REV_V1, report="report:\n  views: [as_recorded, sideways]\n"),
            source="views.yaml",
        )
    assert excinfo.value.code == "contract_report"


def test_contract_error_happens_before_any_batch(tmp_path):
    paths = _make_paths(tmp_path)
    overlapping = _write(
        tmp_path / "points.yaml",
        _contract("""\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-a
                effective:
                  - ["2025-03-01T09:00:00Z", null]
              - version_id: cal-b
                effective:
                  - ["2025-03-01T10:00:00Z", null]
"""),
    )
    _replay(paths["replay"], [_record("2025-03-01T10:00:00Z", 1)])
    with pytest.raises(ContractError):
        _run(paths, overlapping)
    assert not paths["db"].exists()


def test_yaml_datetime_scalars_are_accepted():
    # Unquoted YAML timestamps arrive as datetime objects, not strings.
    contract = parse_contract(
        _contract("""\
          - registered_at: 2025-03-01T08:00:00Z
            versions:
              - version_id: cal-a
                scale: 1
                offset: 0
                effective:
                  - [2025-03-01T09:00:00Z, null]
"""),
        source="yaml-ts.yaml",
    )
    revision = contract.get("p_cal").calibrations[0]
    assert revision.registered_at.hour == 8
    assert revision.versions[0].intervals[0][0].hour == 9


# ---------------------------------------------------------------------------
# Late supplemental sheets, auditable recalculation and the two views
# ---------------------------------------------------------------------------

def _capture_v1(tmp_path, *, range_bounds=(0, 100), report=""):
    paths = _make_paths(tmp_path)
    contract_v1 = _write(
        tmp_path / "points_v1.yaml",
        _contract(REV_V1, range_bounds=range_bounds, report=report,
                  extra_points=STATIC_POINT),
    )
    _replay(paths["replay"], [
        _record("2025-03-01T10:00:00Z", 10),
        _record("2025-03-01T10:05:00Z", 3, start=5),
    ])
    result = _run(paths, contract_v1)
    assert result.status == "completed"
    return paths, contract_v1, result


def test_late_sheet_recalculates_and_two_views_diverge(tmp_path):
    paths, contract_v1, result = _capture_v1(tmp_path)
    batch_id = result.batch_id

    # As captured: coefficient 1 registered at 08:00 -> engineering 10.
    reading = _table(paths["db"], "readings")[0]
    assert reading["engineering_value"] == 10.0
    assert reading["calibration_version_id"] == "cal-v1"
    assert reading["calibration_registered_at"] == "2025-03-01T08:00:00Z"
    frames_before = _table(paths["db"], "frames")
    batch_before = _table(paths["db"], "batches")[0]

    # A calibration sheet arrives late: coefficient 2, effective from 09:00,
    # registered at 12:00 (after the capture).
    contract_v2 = _write(
        tmp_path / "points_v2.yaml",
        _contract(REV_V1 + REV_V2, extra_points=STATIC_POINT),
    )
    recalc = _recalc(paths, contract_v2, batch_id=batch_id)
    assert recalc.considered == 1  # the static point is never recalculated
    assert recalc.derived_created == 1
    assert recalc.unchanged == 0

    # The original reading is not silently updated.
    reading_after = _table(paths["db"], "readings")[0]
    assert reading_after == reading

    # The derived conclusion carries full audit metadata.
    derived = _table(paths["db"], "derived_readings")
    assert len(derived) == 1
    row = derived[0]
    assert row["batch_id"] == batch_id
    assert row["source_reading_id"] == reading["id"]
    assert row["old_calibration_version_id"] == "cal-v1"
    assert row["new_calibration_version_id"] == "cal-v2"
    assert row["new_calibration_registered_at"] == "2025-03-01T12:00:00Z"
    assert row["engineering_value"] == 20.0
    assert row["quality_code"] == "good"
    assert len(row["input_sha256"]) == 64
    assert row["created_at"] == FIXED_NOW

    # The two contract-authorized views diverge, each annotated with the
    # calibration and registration time it used.
    summary = _summary(paths["db"], batch_id, contract_v2)
    views = summary["calibration_views"]
    assert views["authorized_views"] == ["as_recorded", "as_effective"]
    recorded = views["as_recorded"]["readings"][0]
    assert recorded["basis"] == "recorded"
    assert "derivation" not in recorded  # post-hoc sheets are not disguised
    assert recorded["engineering_value"] == 10.0
    assert recorded["calibration_version_id"] == "cal-v1"
    assert recorded["calibration_registered_at"] == "2025-03-01T08:00:00Z"
    effective = views["as_effective"]["readings"][0]
    assert effective["basis"] == "derived"
    assert effective["engineering_value"] == 20.0
    assert effective["calibration_version_id"] == "cal-v2"
    assert effective["calibration_registered_at"] == "2025-03-01T12:00:00Z"
    assert effective["derivation"]["old_calibration_version_id"] == "cal-v1"
    assert effective["derivation"]["new_calibration_version_id"] == "cal-v2"
    assert effective["derivation"]["input_sha256"] == row["input_sha256"]

    # Neither view alters the raw value, CRC evidence or source-file digest.
    assert recorded["raw_value"] == effective["raw_value"] == 10.0
    assert _table(paths["db"], "frames") == frames_before
    batch_after = _table(paths["db"], "batches")[0]
    assert batch_after["replay_sha256"] == batch_before["replay_sha256"]
    assert batch_after["contract_sha256"] == batch_before["contract_sha256"]

    # Reports render both views.
    report_text = (paths["reports"] / "summary.md").read_text(encoding="utf-8")
    assert "校准视图 · as-recorded" in report_text
    assert "校准视图 · as-effective" in report_text
    assert "派生 cal-v1→cal-v2" in report_text
    html = (paths["reports"] / "summary.html").read_text(encoding="utf-8")
    assert "校准视图" in html and "cal-v2" in html


def test_repeated_recalculation_is_idempotent(tmp_path):
    paths, _, result = _capture_v1(tmp_path)
    contract_v2 = _write(
        tmp_path / "points_v2.yaml",
        _contract(REV_V1 + REV_V2, extra_points=STATIC_POINT),
    )
    first = _recalc(paths, contract_v2, batch_id=result.batch_id)
    assert first.derived_created == 1
    before = _table(paths["db"], "derived_readings")

    second = _recalc(paths, contract_v2, batch_id=result.batch_id)
    assert second.derived_created == 0
    assert second.unchanged == 1
    after = _table(paths["db"], "derived_readings")
    assert after == before
    # Original readings are still untouched.
    assert len(_table(paths["db"], "readings")) == 2


def test_superseding_revision_preserves_derived_history(tmp_path):
    paths, _, result = _capture_v1(tmp_path)
    contract_v2 = _write(
        tmp_path / "points_v2.yaml",
        _contract(REV_V1 + REV_V2, extra_points=STATIC_POINT),
    )
    _recalc(paths, contract_v2, batch_id=result.batch_id)

    # An even later sheet covers the same interval with coefficient 3.
    contract_v3 = _write(
        tmp_path / "points_v3.yaml",
        _contract(REV_V1 + REV_V2 + REV_V3, extra_points=STATIC_POINT),
    )
    recalc = _recalc(paths, contract_v3, batch_id=result.batch_id)
    assert recalc.derived_created == 1

    derived = _table(paths["db"], "derived_readings")
    assert len(derived) == 2  # the earlier derived conclusion is preserved
    assert [row["new_calibration_version_id"] for row in derived] == ["cal-v2", "cal-v3"]
    assert derived[1]["old_calibration_version_id"] == "cal-v2"
    assert derived[1]["engineering_value"] == 30.0

    summary = _summary(paths["db"], result.batch_id, contract_v3)
    effective = summary["calibration_views"]["as_effective"]["readings"][0]
    assert effective["engineering_value"] == 30.0
    assert effective["calibration_version_id"] == "cal-v3"
    recorded = summary["calibration_views"]["as_recorded"]["readings"][0]
    assert recorded["engineering_value"] == 10.0
    assert recorded["calibration_version_id"] == "cal-v1"


def test_later_revision_can_revoke_coverage(tmp_path):
    paths, _, result = _capture_v1(tmp_path)
    # The later revision replaces the mapping but only covers from 11:00;
    # the 10:00 frame falls into a gap and must become undeterminable again.
    revoking = """\
          - registered_at: "2025-03-01T12:00:00Z"
            versions:
              - version_id: cal-v2
                scale: 2
                offset: 0
                effective:
                  - ["2025-03-01T11:00:00Z", null]
"""
    contract_v2 = _write(
        tmp_path / "points_v2.yaml",
        _contract(REV_V1 + revoking, extra_points=STATIC_POINT),
    )
    recalc = _recalc(paths, contract_v2, batch_id=result.batch_id)
    assert recalc.derived_created == 1

    derived = _table(paths["db"], "derived_readings")[0]
    assert derived["old_calibration_version_id"] == "cal-v1"
    assert derived["new_calibration_version_id"] == ""
    assert derived["engineering_value"] is None
    assert derived["quality_code"] == "calibration_unknown"
    assert derived["new_calibration_registered_at"] == "2025-03-01T12:00:00Z"

    summary = _summary(paths["db"], result.batch_id, contract_v2)
    views = summary["calibration_views"]
    effective = views["as_effective"]["readings"][0]
    assert effective["engineering_value"] is None
    assert effective["quality_code"] == "calibration_unknown"
    assert effective["basis"] == "derived"
    assert views["as_effective"]["totals"]["calibration_unknown"] == 1
    assert views["as_effective"]["totals"]["pending_points"] == 1
    # The recorded view is untouched.
    assert views["as_recorded"]["readings"][0]["engineering_value"] == 10.0
    assert views["as_recorded"]["totals"]["valid_readings"] == 1


def test_view_stats_follow_calibration_causally(tmp_path):
    # Coefficient 2 pushes the value above the range maximum of 15.
    paths, _, result = _capture_v1(tmp_path, range_bounds=(0, 15))
    contract_v2 = _write(
        tmp_path / "points_v2.yaml",
        _contract(REV_V1 + REV_V2, range_bounds=(0, 15), extra_points=STATIC_POINT),
    )
    _recalc(paths, contract_v2, batch_id=result.batch_id)
    summary = _summary(paths["db"], result.batch_id, contract_v2)
    views = summary["calibration_views"]

    recorded_totals = views["as_recorded"]["totals"]
    assert recorded_totals["valid_readings"] == 1
    assert recorded_totals["out_of_range"] == 0
    effective_totals = views["as_effective"]["totals"]
    assert effective_totals["valid_readings"] == 0
    assert effective_totals["out_of_range"] == 1
    assert effective_totals["derived_readings"] == 1

    # The derived row carries the new quality code; no view mixes a new value
    # with a stale quality code.
    effective_row = views["as_effective"]["readings"][0]
    assert effective_row["engineering_value"] == 20.0
    assert effective_row["quality_code"] == "overflow"
    derived = _table(paths["db"], "derived_readings")[0]
    assert derived["quality_code"] == "overflow"


def test_late_sheet_covering_a_gap_clears_pending(tmp_path):
    paths = _make_paths(tmp_path)
    gap_revision = """\
          - registered_at: "2025-03-01T08:00:00Z"
            versions:
              - version_id: cal-v1
                scale: 1
                offset: 0
                effective:
                  - ["2025-03-01T00:00:00Z", "2025-03-01T09:00:00Z"]
"""
    contract_v1 = _write(tmp_path / "points_v1.yaml", _contract(gap_revision))
    _replay(paths["replay"], [_record("2025-03-01T10:00:00Z", 10)])
    result = _run(paths, contract_v1)

    summary = _summary(paths["db"], result.batch_id, contract_v1)
    recorded_totals = summary["calibration_views"]["as_recorded"]["totals"]
    assert recorded_totals["calibration_unknown"] == 1
    assert recorded_totals["pending_points"] == 1
    assert recorded_totals["valid_readings"] == 0

    # The late sheet covers the previously undeterminable interval.
    contract_v2 = _write(
        tmp_path / "points_v2.yaml", _contract(gap_revision + REV_V2)
    )
    _recalc(paths, contract_v2, batch_id=result.batch_id)
    summary = _summary(paths["db"], result.batch_id, contract_v2)
    effective_totals = summary["calibration_views"]["as_effective"]["totals"]
    assert effective_totals["calibration_unknown"] == 0
    assert effective_totals["pending_points"] == 0
    assert effective_totals["valid_readings"] == 1
    row = summary["calibration_views"]["as_effective"]["readings"][0]
    assert row["engineering_value"] == 20.0
    assert row["quality_code"] == "good"
    assert row["derivation"]["old_calibration_version_id"] == ""


def test_bad_crc_frames_are_never_recalculated(tmp_path):
    paths = _make_paths(tmp_path)
    contract_v1 = _write(tmp_path / "points_v1.yaml", _contract(REV_V1))
    _replay(paths["replay"], [_record("2025-03-01T10:00:00Z", 10, corrupt=True)])
    result = _run(paths, contract_v1)
    assert result.reading_count == 0

    frame = _table(paths["db"], "frames")[0]
    assert frame["quality_code"] == "bad_crc_response"
    assert frame["response_crc_ok"] == 0

    contract_v2 = _write(
        tmp_path / "points_v2.yaml", _contract(REV_V1 + REV_V2)
    )
    recalc = _recalc(paths, contract_v2, batch_id=result.batch_id)
    assert recalc.considered == 0
    assert recalc.derived_created == 0
    assert _table(paths["db"], "derived_readings") == []
    # Frame-level evidence is preserved byte-for-byte.
    assert _table(paths["db"], "frames")[0] == frame


def test_contract_authorizes_a_single_view(tmp_path):
    paths, _, result = _capture_v1(
        tmp_path, report="report:\n  views: [as_recorded]\n"
    )
    contract_v2 = _write(
        tmp_path / "points_v2.yaml",
        _contract(REV_V1 + REV_V2, report="report:\n  views: [as_recorded]\n",
                  extra_points=STATIC_POINT),
    )
    _recalc(paths, contract_v2, batch_id=result.batch_id)
    summary = _summary(paths["db"], result.batch_id, contract_v2)
    views = summary["calibration_views"]
    assert views["authorized_views"] == ["as_recorded"]
    assert "as_recorded" in views
    assert "as_effective" not in views


# ---------------------------------------------------------------------------
# Interruption and recovery consistency
# ---------------------------------------------------------------------------

def _three_reading_setup(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    paths = _make_paths(root)
    contract_v1 = _write(
        paths["replay"].parent / "points_v1.yaml", _contract(REV_V1)
    )
    _replay(paths["replay"], [
        _record("2025-03-01T10:00:00Z", 10),
        _record("2025-03-01T10:05:00Z", 11),
        _record("2025-03-01T10:10:00Z", 12),
    ])
    result = _run(paths, contract_v1)
    assert result.reading_count == 3
    contract_v2 = _write(
        paths["replay"].parent / "points_v2.yaml", _contract(REV_V1 + REV_V2)
    )
    return paths, result.batch_id, contract_v2


def test_interrupted_recalibration_resumes_consistently(tmp_path):
    paths, batch_id, contract_v2 = _three_reading_setup(tmp_path / "interrupted")

    with pytest.raises(RecalibrationInterrupted):
        _recalc(paths, contract_v2, batch_id=batch_id, stop_after=1)
    assert len(_table(paths["db"], "derived_readings")) == 1

    resumed = _recalc(paths, contract_v2, batch_id=batch_id)
    assert resumed.considered == 3
    assert resumed.derived_created == 2
    assert resumed.unchanged == 1

    # A fresh uninterrupted run must converge to the identical ledger state.
    other_paths, other_batch_id, other_contract_v2 = _three_reading_setup(
        tmp_path / "uninterrupted"
    )
    _recalc(other_paths, other_contract_v2, batch_id=other_batch_id)

    assert _table(paths["db"], "derived_readings") == _table(
        other_paths["db"], "derived_readings"
    )
    assert _table(paths["db"], "readings") == _table(other_paths["db"], "readings")


def test_capture_resume_with_calibrations_is_consistent(tmp_path):
    def fresh_capture(root):
        root.mkdir(exist_ok=True)
        paths = _make_paths(root)
        contract = _write(root / "points.yaml", _contract(BOUNDARY_REVISION))
        _replay(paths["replay"], [
            _record("2025-03-01T09:00:00Z", 1),
            _record("2025-03-01T10:00:00Z", 1),
            _record("2025-03-01T11:00:00Z", 1),
        ])
        return paths, contract

    paths, contract = fresh_capture(tmp_path / "interrupted")
    with pytest.raises(IngestInterrupted):
        _run(paths, contract, stop_after=1)
    result = _run(paths, contract, resume=True)
    assert result.status == "completed"

    clean_paths, clean_contract = fresh_capture(tmp_path / "clean")
    clean_result = _run(clean_paths, clean_contract)
    assert clean_result.status == "completed"

    for table in ("readings", "frames", "issues"):
        assert _table(paths["db"], table) == _table(clean_paths["db"], table)
