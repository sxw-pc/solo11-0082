"""White-box tests for the pure three-valued evaluator (no SQLite involved)."""

from pointlog.contract import parse_contract
from pointlog.interlock import (
    BadEvidence,
    Sample,
    evaluate_rule,
    format_epoch,
    parse_timestamp,
)

CONTRACT = """version: 1
devices:
  - id: plant-a
    label: Plant A
    points:
      - key: temp
        slave_id: 1
        function: 3
        start: 0
        count: 1
        data_type: int16
        byte_order: AB
        scale: 0.1
        offset: 0
        unit: degC
        range: [-40, 150]
      - key: pressure
        slave_id: 1
        function: 3
        start: 1
        count: 1
        data_type: uint16
        byte_order: AB
        scale: 0.1
        offset: 0
        unit: kPa
        range: [0, 1000]
    interlocks:
      - id: hp
        version: 7
        points:
          - {key: temp, threshold: 60, direction: above}
          - {key: pressure, threshold: 800, direction: above}
        min_duration_minutes: 2
        max_stale_minutes: 5
        sampling: __SAMPLING__
"""


def _rule(sampling="bounded_hold", **overrides):
    text = CONTRACT.replace("__SAMPLING__", sampling)
    rule = parse_contract(text).interlocks[0]
    if overrides:
        from dataclasses import replace
        rule = replace(rule, **overrides)
    return rule


def _sample(minute, value, line):
    when = f"2025-03-01T00:{minute:02d}:00Z"
    return Sample(parse_timestamp(when), when, line, value)


def _run(rule, samples, bad=None):
    return evaluate_rule(rule, samples, bad or {})


def test_parse_timestamp_handles_offsets_naive_and_garbage():
    assert parse_timestamp("2025-03-01T00:00:00Z") == parse_timestamp(
        "2025-03-01T00:00:00+00:00"
    )
    # Naive timestamps are UTC so results never depend on host timezone.
    assert parse_timestamp("2025-03-01T00:00:00") == parse_timestamp(
        "2025-03-01T00:00:00Z"
    )
    assert parse_timestamp("") is None
    assert parse_timestamp("not-a-time") is None
    assert parse_timestamp(None) is None
    assert format_epoch(parse_timestamp("2025-03-01T00:02:00Z")) == (
        "2025-03-01T00:02:00Z"
    )


def test_three_valued_and_semantics():
    rule = _rule()
    # Two instants are needed for a measurable window.
    both_true = {
        "temp": [_sample(0, 70, 1), _sample(2, 50, 5)],
        "pressure": [_sample(0, 900, 2)],
    }
    evaluation = _run(rule, both_true)
    assert evaluation.intervals[0]["state"] == "true"

    # One explicit false wins over an unknown counterpart.
    one_false = {
        "temp": [_sample(0, 70, 1), _sample(2, 50, 5)],
        "pressure": [_sample(0, 500, 2)],
    }
    evaluation = _run(rule, one_false)
    assert {item["state"] for item in evaluation.intervals} == {"false"}

    # True plus unknown is unknown, never true.
    half_known = {"temp": [_sample(0, 70, 1), _sample(2, 50, 5)]}
    evaluation = _run(rule, half_known)
    assert {item["state"] for item in evaluation.intervals} == {"unknown"}


def test_inclusive_and_below_direction_are_honoured():
    rule = _rule()
    from dataclasses import replace
    from pointlog.contract import DIRECTION_BELOW, InterlockPoint
    rule = replace(
        rule,
        points=(
            InterlockPoint("temp", 70.0, DIRECTION_BELOW, True),
            InterlockPoint("pressure", 800.0, "above", False),
        ),
    )
    samples = {
        "temp": [_sample(0, 70, 1), _sample(2, 80, 5)],
        "pressure": [_sample(0, 800, 2)],
    }
    evaluation = _run(rule, samples)
    # temp 70 <= 70 (inclusive) true; pressure 800 > 800 false -> explicit false.
    assert evaluation.intervals[0]["state"] == "false"
    samples["pressure"] = [_sample(0, 801, 3)]
    assert _run(rule, samples).intervals[0]["state"] == "true"


def test_event_requires_continuous_coverage_of_the_threshold():
    rule = _rule()
    records = {"temp": [_sample(0, 70, 1)], "pressure": [_sample(0, 900, 2)]}
    evaluation = _run(rule, records)
    assert evaluation.events == []  # only 5 s of coverage from a single instant
    records = {
        "temp": [_sample(0, 70, 1), _sample(3, 50, 3)],
        "pressure": [_sample(0, 900, 2), _sample(4, 900, 4)],
    }
    evaluation = _run(rule, records)
    assert len(evaluation.events) == 1
    assert evaluation.events[0]["duration_seconds"] == 180.0


def test_event_reports_rule_version_peaks_and_interruption():
    rule = _rule()
    records = {
        "temp": [_sample(0, 70, 1), _sample(2, 75, 3), _sample(4, 50, 5)],
        "pressure": [_sample(0, 900, 2), _sample(2, 950, 4), _sample(6, 800, 6)],
    }
    evaluation = _run(rule, records)
    event = evaluation.events[0]
    assert event["rule_version"] == 7
    assert event["peak_a"] == 75
    assert event["peak_b"] == 950
    assert event["interruption_reason"] == "explicit_normal"
    assert event["interruption_lines"] == [5]


def test_bad_evidence_at_the_same_instant_is_conflicting():
    rule = _rule()
    bad = {"temp": [BadEvidence(parse_timestamp("2025-03-01T00:00:00Z"),
                                "2025-03-01T00:00:00Z", 9, "bad_crc_response")]}
    records = {
        "temp": [_sample(0, 70, 1), _sample(2, 50, 5)],
        "pressure": [_sample(0, 900, 2)],
    }
    evaluation = _run(rule, records, bad)
    assert evaluation.events == []
    # The conflicting instant is dropped, so the window opens unknown.
    assert evaluation.intervals[0]["state"] == "unknown"
    assert evaluation.intervals[0]["reason"] == "conflicting_values"
    assert 9 in evaluation.intervals[0]["line_numbers"]


def test_duplicate_agreeing_samples_are_collapsed():
    rule = _rule()
    records = {
        "temp": [_sample(0, 70, 1), _sample(0, 70, 2),
                 _sample(5, 50, 3)],
        "pressure": [_sample(0, 900, 4), _sample(5, 900, 5)],
    }
    evaluation = _run(rule, records)
    # Duplicate agrees, so the interval remains true rather than conflicting.
    assert evaluation.intervals[0]["state"] == "true"


def test_strict_mode_within_one_timestamp_never_produces_duration():
    rule = _rule("strict_simultaneous")
    records = {
        "temp": [_sample(0, 70, 1), _sample(4, 75, 3)],
        "pressure": [_sample(0, 900, 2), _sample(4, 950, 4)],
    }
    evaluation = _run(rule, records)
    assert evaluation.events == []
    assert evaluation.totals["proven_simultaneous_instants"] == 2
    assert evaluation.totals["proven_interlock_seconds"] == 0
    unknown = [item for item in evaluation.intervals if item["state"] == "unknown"]
    assert unknown and all(
        item["reason"] == "no_simultaneous_sample" for item in unknown
    )


def test_empty_evidence_produces_no_window():
    evaluation = _run(_rule(), {})
    assert evaluation.intervals == []
    assert evaluation.events == []
    assert evaluation.window_start_ts is None
    assert evaluation.totals["window_seconds"] == 0
