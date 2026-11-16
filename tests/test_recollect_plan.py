import json

import pytest

from pointlog.cli import main
from pointlog.contract import parse_contract
from pointlog.modbus import build_request, build_response
from pointlog.pipeline import IngestInterrupted, run_capture
from pointlog.recollect_plan import build_recollect_plan
from pointlog.store import Store

BASE_TIME = "2026-01-01T00:00:00Z"
RECOLLECT = (
    "recollect:\n"
    "  objective: coverage\n"
    "  max_requests: 10\n"
    "  device_cooldown_s: 0\n"
    "  request_cost_s: 1\n"
)


def make_contract(points, recollect=RECOLLECT):
    """Build a contract from point specs.

    Each spec needs key/slave_id/function/start/count and may set
    device/priority/ttl.  count=1 maps to int16, count=2 to int32.
    """
    devices: dict[str, list[dict]] = {}
    for spec in points:
        devices.setdefault(spec.get("device", "dev"), []).append(spec)
    lines = ["version: 1"]
    if recollect is not None:
        lines.append(recollect.rstrip("\n"))
    lines.append("devices:")
    for device_id, specs in devices.items():
        lines.append(f"  - id: {device_id}")
        lines.append("    points:")
        for spec in specs:
            count = spec["count"]
            lines += [
                f"      - key: {spec['key']}",
                f"        slave_id: {spec['slave_id']}",
                f"        function: {spec['function']}",
                f"        start: {spec['start']}",
                f"        count: {count}",
                f"        data_type: {'int16' if count == 1 else 'int32'}",
                f"        byte_order: {'AB' if count == 1 else 'ABCD'}",
                "        range: [0, 1000]",
            ]
            if spec.get("priority") is not None:
                lines.append(f"        recollect_priority: {spec['priority']}")
            if spec.get("ttl") is not None:
                lines.append(f"        recollect_ttl_seconds: {spec['ttl']}")
    return parse_contract("\n".join(lines) + "\n", source="test.yaml")


def recollect_yaml(**overrides):
    values = {
        "objective": "coverage",
        "max_requests": 10,
        "device_cooldown_s": 0,
        "request_cost_s": 1,
    }
    values.update(overrides)
    lines = ["recollect:"]
    for key, value in values.items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def pending(*refs):
    """Build pending-issue rows like store_queries.pending_recollect returns.

    Each ref is (device, point_key) or (device, point_key, [issue_codes]).
    """
    items = []
    for ref in refs:
        device, key = ref[0], ref[1]
        issue_codes = ref[2] if len(ref) > 2 else ["missing_response"]
        for code in issue_codes:
            items.append({
                "device_id": device,
                "point_key": key,
                "issue_code": code,
                "quality_code": code,
                "severity": "warning",
                "occurrences": 1,
                "line_numbers": [],
                "detail": "synthetic",
            })
    return items


def plan_for(contract, items, **kwargs):
    kwargs.setdefault("base_time", BASE_TIME)
    return build_recollect_plan(items, contract, **kwargs)


def test_adjacent_points_merge_into_single_request():
    contract = make_contract([
        {"key": "a", "slave_id": 1, "function": 3, "start": 0, "count": 2},
        {"key": "b", "slave_id": 1, "function": 3, "start": 2, "count": 1},
        {"key": "c", "slave_id": 1, "function": 3, "start": 3, "count": 1},
    ])
    plan = plan_for(contract, pending(("dev", "a"), ("dev", "b"), ("dev", "c")))
    assert len(plan["requests"]) == 1
    request = plan["requests"][0]
    assert (request["slave_id"], request["function"]) == (1, 3)
    assert (request["start"], request["count"]) == (0, 4)
    assert request["request_hex"] == build_request(1, 3, 0, 4).hex()
    assert [item["point_key"] for item in request["covers"]] == ["a", "b", "c"]
    usage = plan["usage"]
    assert usage["requests_used"] == 1
    assert usage["covered_points"] == 3
    assert usage["independent_failure_chances"] == 1
    assert {point["point_key"]: point["covered_by"] for point in plan["points"]} == {
        "a": "r1", "b": "r1", "c": "r1",
    }


def test_merge_never_crosses_slave_or_function():
    contract = make_contract([
        {"key": "a", "slave_id": 1, "function": 3, "start": 0, "count": 1},
        {"key": "b", "slave_id": 1, "function": 3, "start": 1, "count": 1},
        # Adjacent address but a different slave: must stay separate.
        {"key": "c", "slave_id": 2, "function": 3, "start": 2, "count": 1},
        # Same slave but a different function code: must stay separate.
        {"key": "d", "slave_id": 1, "function": 4, "start": 2, "count": 1},
    ])
    plan = plan_for(
        contract,
        pending(("dev", "a"), ("dev", "b"), ("dev", "c"), ("dev", "d")),
    )
    spans = {
        (r["slave_id"], r["function"], r["start"], r["count"])
        for r in plan["requests"]
    }
    assert spans == {(1, 3, 0, 2), (2, 3, 2, 1), (1, 4, 2, 1)}
    assert plan["usage"]["independent_failure_chances"] == 3


def test_overlapping_logical_points_share_one_physical_request():
    contract = make_contract([
        {"key": "wide", "slave_id": 1, "function": 3, "start": 0, "count": 2},
        {"key": "narrow", "slave_id": 1, "function": 3, "start": 1, "count": 1},
        # Non-pending logical view of the same physical register 0.
        {"key": "alias", "slave_id": 1, "function": 3, "start": 0, "count": 1},
    ])
    plan = plan_for(contract, pending(("dev", "wide"), ("dev", "narrow")))
    assert len(plan["requests"]) == 1
    request = plan["requests"][0]
    assert (request["start"], request["count"]) == (0, 2)
    assert sorted(item["point_key"] for item in request["covers"]) == [
        "narrow", "wide",
    ]
    assert request["incidental_points"] == ["alias"]
    usage = plan["usage"]
    # Physical requests and logical obligations are counted separately:
    # one request occupies the budget once but fulfils two obligations.
    assert usage["requests_used"] == 1
    assert usage["covered_points"] == 2
    assert usage["independent_failure_chances"] == 1
    keys = {point["point_key"] for point in plan["points"]}
    assert "alias" not in keys  # non-pending points never become targets


def test_multiple_issues_on_one_point_form_a_single_obligation():
    contract = make_contract([
        {"key": "a", "slave_id": 1, "function": 3, "start": 0, "count": 1},
    ])
    items = pending(("dev", "a", ["bad_frame", "bad_crc_response"]))
    items[0]["occurrences"] = 2
    items[0]["line_numbers"] = [7]
    plan = plan_for(contract, items)
    assert plan["usage"]["pending_points"] == 1
    point = plan["points"][0]
    assert point["issue_codes"] == ["bad_crc_response", "bad_frame"]
    assert point["occurrences"] == 3
    assert point["line_numbers"] == [7]
    assert point["status"] == "covered"
    assert plan["usage"]["requests_used"] == 1


def test_objectives_diverge_when_budget_allows_only_one_request():
    points = [
        {"key": "critical", "slave_id": 2, "function": 3, "start": 0,
         "count": 1, "priority": 9, "ttl": 3600},
        {"key": "low1", "slave_id": 1, "function": 3, "start": 10,
         "count": 1, "priority": 1},
        {"key": "low2", "slave_id": 1, "function": 3, "start": 11,
         "count": 1, "priority": 1},
        {"key": "low3", "slave_id": 1, "function": 3, "start": 12,
         "count": 1, "priority": 1},
    ]
    refs = pending(
        ("dev", "critical"), ("dev", "low1"), ("dev", "low2"), ("dev", "low3")
    )
    budget_one = {"max_requests": 1}

    priority_contract = make_contract(
        points, recollect_yaml(objective="priority", **budget_one)
    )
    plan = plan_for(priority_contract, refs)
    assert len(plan["requests"]) == 1
    assert [item["point_key"] for item in plan["requests"][0]["covers"]] == [
        "critical"
    ]
    assert plan["requests"][0]["count"] == 1

    coverage_contract = make_contract(
        points, recollect_yaml(objective="coverage", **budget_one)
    )
    plan = plan_for(coverage_contract, refs)
    assert len(plan["requests"]) == 1
    request = plan["requests"][0]
    assert (request["start"], request["count"]) == (10, 3)
    assert sorted(item["point_key"] for item in request["covers"]) == [
        "low1", "low2", "low3",
    ]
    assert plan["usage"]["covered_points"] == 3
    blocked = {
        point["point_key"]: point["block"]["code"]
        for point in plan["points"]
        if point["status"] == "uncovered"
    }
    assert blocked == {"critical": "request_budget_exceeded"}


def test_budget_exhaustion_selects_instead_of_truncating():
    contract = make_contract(
        [
            {"key": "zz_solo", "slave_id": 1, "function": 3, "start": 0,
             "count": 1},
            {"key": "aa_pair1", "slave_id": 2, "function": 3, "start": 0,
             "count": 1},
            {"key": "aa_pair2", "slave_id": 2, "function": 3, "start": 1,
             "count": 1},
        ],
        recollect_yaml(max_requests=1),
    )
    # Pending-list order (alphabetical) would truncate to aa_pair1 alone;
    # the coverage objective must instead pick the merged pair.
    plan = plan_for(
        contract,
        pending(("dev", "zz_solo"), ("dev", "aa_pair1"), ("dev", "aa_pair2")),
    )
    assert len(plan["requests"]) == 1
    assert sorted(item["point_key"] for item in plan["requests"][0]["covers"]) == [
        "aa_pair1", "aa_pair2",
    ]
    blocked = {
        point["point_key"]: point["block"]["code"]
        for point in plan["points"]
        if point["status"] == "uncovered"
    }
    assert blocked == {"zz_solo": "request_budget_exceeded"}


def test_cooldown_can_push_requests_out_of_the_window():
    contract = make_contract(
        [
            {"key": "a", "slave_id": 1, "function": 3, "start": 0, "count": 1},
            # Unmapped registers 1..9 forbid merging; same slave => cooldown.
            {"key": "b", "slave_id": 1, "function": 3, "start": 10, "count": 1},
        ],
        recollect_yaml(
            max_requests=5, device_cooldown_s=10, request_cost_s=1,
            window_seconds=1.5,
        ),
    )
    plan = plan_for(contract, pending(("dev", "a"), ("dev", "b")))
    assert len(plan["requests"]) == 1
    request = plan["requests"][0]
    assert request["scheduled_start_s"] == 0
    assert request["scheduled_end_s"] == 1
    uncovered = [
        point for point in plan["points"] if point["status"] == "uncovered"
    ]
    assert len(uncovered) == 1
    assert uncovered[0]["point_key"] == "b"
    assert uncovered[0]["block"]["code"] == "window_time_exceeded"


def test_window_blocked_candidate_does_not_block_other_devices():
    contract = make_contract(
        [
            {"key": "a", "slave_id": 1, "function": 3, "start": 0, "count": 1},
            {"key": "b", "slave_id": 1, "function": 3, "start": 10, "count": 1},
            {"key": "c", "slave_id": 2, "function": 3, "start": 0, "count": 1},
        ],
        recollect_yaml(
            max_requests=5, device_cooldown_s=10, request_cost_s=1,
            window_seconds=2.5,
        ),
    )
    plan = plan_for(
        contract, pending(("dev", "a"), ("dev", "b"), ("dev", "c"))
    )
    # b is pushed out of the window by slave-1 cooldown, but c (slave 2)
    # still fits right after a on the shared bus.
    covered = {
        item["point_key"]
        for request in plan["requests"]
        for item in request["covers"]
    }
    assert covered == {"a", "c"}
    blocked = {
        point["point_key"]: point["block"]["code"]
        for point in plan["points"]
        if point["status"] == "uncovered"
    }
    assert blocked == {"b": "window_time_exceeded"}
    assert plan["usage"]["window_used_s"] == 2


def test_zero_budget_gives_every_point_a_concrete_reason():
    contract = make_contract(
        [
            {"key": "a", "slave_id": 1, "function": 3, "start": 0, "count": 1},
            {"key": "b", "slave_id": 1, "function": 3, "start": 1, "count": 1},
        ],
        recollect_yaml(max_requests=0),
    )
    plan = plan_for(contract, pending(("dev", "a"), ("dev", "b")))
    assert plan["requests"] == []
    assert plan["usage"]["requests_used"] == 0
    assert plan["usage"]["independent_failure_chances"] == 0
    assert plan["usage"]["covered_points"] == 0
    for point in plan["points"]:
        assert point["status"] == "uncovered"
        assert point["covered_by"] is None
        assert point["block"]["code"] == "request_budget_exceeded"
        assert point["block"]["detail"]


def test_unmapped_registers_are_forbidden_gaps():
    specs = [
        {"key": "a", "slave_id": 1, "function": 3, "start": 0, "count": 1},
        {"key": "b", "slave_id": 1, "function": 3, "start": 3, "count": 1},
    ]
    contract = make_contract(specs)
    plan = plan_for(contract, pending(("dev", "a"), ("dev", "b")))
    assert len(plan["requests"]) == 2  # registers 1-2 are unmapped: no merge

    filled = make_contract(specs + [
        {"key": "fill1", "slave_id": 1, "function": 3, "start": 1, "count": 1},
        {"key": "fill2", "slave_id": 1, "function": 3, "start": 2, "count": 1},
    ])
    plan = plan_for(filled, pending(("dev", "a"), ("dev", "b")))
    assert len(plan["requests"]) == 1
    request = plan["requests"][0]
    assert (request["start"], request["count"]) == (0, 4)
    assert request["incidental_points"] == ["fill1", "fill2"]
    # Gap points are physically re-read but are not recollect targets.
    assert plan["usage"]["covered_points"] == 2
    assert {point["point_key"] for point in plan["points"]} == {"a", "b"}


def test_request_span_never_exceeds_125_registers():
    fills = [
        {"key": f"fill{i}", "slave_id": 1, "function": 3, "start": i, "count": 1}
        for i in range(2, 124)
    ]
    contract = make_contract(
        [{"key": "first", "slave_id": 1, "function": 3, "start": 0, "count": 2}]
        + fills
        + [{"key": "last", "slave_id": 1, "function": 3, "start": 124, "count": 2}]
    )
    plan = plan_for(contract, pending(("dev", "first"), ("dev", "last")))
    # Merged span would be 126 registers: must split into two requests.
    assert len(plan["requests"]) == 2
    assert all(request["count"] <= 125 for request in plan["requests"])
    assert plan["usage"]["covered_points"] == 2

    within = make_contract(
        [{"key": "first", "slave_id": 1, "function": 3, "start": 0, "count": 2}]
        + fills[:-2]
        + [{"key": "last", "slave_id": 1, "function": 3, "start": 122, "count": 2}]
    )
    plan = plan_for(within, pending(("dev", "first"), ("dev", "last")))
    assert len(plan["requests"]) == 1  # span 124 fits
    assert plan["requests"][0]["count"] == 124


def test_points_missing_from_current_contract_get_concrete_reasons():
    contract = make_contract([
        {"key": "known", "slave_id": 1, "function": 3, "start": 0, "count": 1},
    ])
    items = pending(("dev", "known"), ("dev", "ghost"), ("", ""))
    plan = plan_for(contract, items)
    blocks = {
        point["point_key"]: point["block"]["code"]
        for point in plan["points"]
        if point["status"] == "uncovered"
    }
    assert blocks == {"ghost": "point_not_in_contract", "": "point_not_in_contract"}
    assert len(plan["requests"]) == 1  # the known point is still planned


def test_device_mismatch_between_issue_and_contract_blocks_planning():
    contract = make_contract([
        {"key": "a", "device": "devA", "slave_id": 1, "function": 3,
         "start": 0, "count": 1},
    ])
    plan = plan_for(contract, pending(("devB", "a")))
    point = plan["points"][0]
    assert point["status"] == "uncovered"
    assert point["block"]["code"] == "contract_device_mismatch"
    assert plan["requests"] == []


def test_priority_objective_prefers_earliest_expiring_high_priority():
    contract = make_contract(
        [
            {"key": "fresh", "slave_id": 1, "function": 3, "start": 0,
             "count": 1, "priority": 5, "ttl": 7200},
            {"key": "stale", "slave_id": 2, "function": 3, "start": 0,
             "count": 1, "priority": 5, "ttl": 60},
            {"key": "unprioritized", "slave_id": 3, "function": 3, "start": 0,
             "count": 1},
        ],
        recollect_yaml(objective="priority", max_requests=2),
    )
    plan = plan_for(
        contract,
        pending(("dev", "fresh"), ("dev", "stale"), ("dev", "unprioritized")),
    )
    covered = {
        item["point_key"]
        for request in plan["requests"]
        for item in request["covers"]
    }
    assert covered == {"stale", "fresh"}  # same priority: earliest expiry first
    expires = {
        point["point_key"]: point["expires_at"] for point in plan["points"]
    }
    assert expires["stale"] == "2026-01-01T00:01:00Z"
    assert expires["fresh"] == "2026-01-01T02:00:00Z"
    assert expires["unprioritized"] is None


def test_plan_is_byte_stable_across_repeated_runs():
    contract = make_contract(
        [
            {"key": "a", "slave_id": 1, "function": 3, "start": 0, "count": 2,
             "priority": 5, "ttl": 3600},
            {"key": "b", "slave_id": 1, "function": 3, "start": 2, "count": 1},
            {"key": "c", "slave_id": 2, "function": 4, "start": 10, "count": 1,
             "priority": 1, "ttl": 60},
        ],
        recollect_yaml(max_requests=2, device_cooldown_s=0.25,
                       request_cost_s=0.05, window_seconds=30),
    )
    items = pending(
        ("dev", "a", ["bad_frame"]), ("dev", "b"), ("dev", "c", ["bad_crc_response"])
    )
    first = json.dumps(plan_for(contract, items), ensure_ascii=False, indent=2)
    second = json.dumps(plan_for(contract, items), ensure_ascii=False, indent=2)
    assert first == second


def test_summary_references_selected_coverage(demo_run):
    summary = json.loads(
        (demo_run.paths.reports / "summary.json").read_text(encoding="utf-8")
    )
    plan = summary["recollect_plan"]
    usage = plan["usage"]
    assert usage["pending_points"] == 7
    assert usage["covered_points"] + usage["uncovered_points"] == 7
    assert usage["independent_failure_chances"] == len(plan["requests"])
    request_ids = {request["id"] for request in plan["requests"]}
    covered = [point for point in plan["points"] if point["status"] == "covered"]
    assert covered
    assert all(point["covered_by"] in request_ids for point in covered)
    for request in plan["requests"]:
        # A failed request impacts every covered point at once; it still
        # counts as a single independent failure chance.
        assert request["failure_impact_points"] == len(request["covers"])
    uncovered = [point for point in plan["points"] if point["status"] == "uncovered"]
    assert all(point["block"]["code"] for point in uncovered)


def test_summary_without_recollect_section_keeps_legacy_shape(tmp_path):
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(
        "version: 1\n"
        "devices:\n"
        "  - id: dev\n"
        "    points:\n"
        "      - key: only\n"
        "        slave_id: 1\n"
        "        function: 3\n"
        "        start: 0\n"
        "        count: 1\n"
        "        data_type: int16\n"
        "        byte_order: AB\n"
        "        range: [0, 1000]\n",
        encoding="utf-8",
    )
    replay_path = tmp_path / "replay.jsonl"
    replay_path.write_text(
        json.dumps({
            "ts": "2026-01-01T00:00:00Z",
            "request_hex": build_request(1, 3, 0, 1).hex(),
            "response_hex": build_response(1, 3, b"\x00\x2a").hex(),
            "source": "test",
        }) + "\n",
        encoding="utf-8",
    )
    run_capture(
        replay_path=replay_path,
        contract_path=contract_path,
        db_path=tmp_path / "pointlog.db",
        reports_dir=tmp_path / "reports",
    )
    summary = json.loads(
        (tmp_path / "reports" / "summary.json").read_text(encoding="utf-8")
    )
    assert "recollect_plan" not in summary
    assert list(summary) == [
        "schema_version", "batch", "totals", "quality_code_distribution",
        "issue_distribution", "devices", "points", "pending_recollect",
    ]


def test_cli_plan_json_text_and_overrides(demo_run, capsys):
    db = str(demo_run.paths.db)
    batch_id = demo_run.result.batch_id
    assert main(["plan", batch_id, "--db", db, "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["objective"] == "coverage"
    assert plan["usage"]["pending_points"] == 7

    assert main(["plan", batch_id, "--db", db]) == 0
    text = capsys.readouterr().out
    assert "重采计划" in text
    assert "独立失败机会" in text
    assert "r1" in text

    assert main([
        "plan", batch_id, "--db", db,
        "--objective", "priority", "--max-requests", "1", "--json",
    ]) == 0
    overridden = json.loads(capsys.readouterr().out)
    assert overridden["objective"] == "priority"
    assert overridden["budget"]["max_requests"] == 1
    assert len(overridden["requests"]) == 1
    # Highest-priority pending point in the demo contract is line_a_speed.
    assert [item["point_key"] for item in overridden["requests"][0]["covers"]] == [
        "line_a_speed"
    ]


def test_cli_plan_requires_completed_batch(demo_paths, capsys):
    with pytest.raises(IngestInterrupted):
        run_capture(
            replay_path=demo_paths.fixture,
            contract_path=demo_paths.contract,
            db_path=demo_paths.db,
            reports_dir=None,
            stop_after=3,
        )
    store = Store(demo_paths.db, read_only=True)
    try:
        batch = store.find_latest_batch_for_path(str(demo_paths.fixture.resolve()))
    finally:
        store.close()
    assert batch["status"] == "partial"
    assert main(["plan", batch["id"], "--db", str(demo_paths.db)]) == 4
    assert "未完成" in capsys.readouterr().err


def test_cli_plan_requires_recollect_section(demo_paths, tmp_path, capsys):
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        "version: 1\ndevices:\n  - id: dev\n    points:\n"
        "      - key: only\n        slave_id: 1\n        function: 3\n"
        "        start: 0\n        count: 1\n        data_type: int16\n"
        "        byte_order: AB\n        range: [0, 1000]\n",
        encoding="utf-8",
    )
    replay = tmp_path / "replay.jsonl"
    replay.write_text(
        json.dumps({
            "ts": "2026-01-01T00:00:00Z",
            "request_hex": build_request(1, 3, 0, 1).hex(),
            "response_hex": build_response(1, 3, b"\x00\x2a").hex(),
            "source": "test",
        }) + "\n",
        encoding="utf-8",
    )
    db = tmp_path / "pointlog.db"
    result = run_capture(
        replay_path=replay, contract_path=legacy, db_path=db, reports_dir=None
    )
    assert main(["plan", result.batch_id, "--db", str(db)]) == 2
    assert "recollect" in capsys.readouterr().err


def test_cli_plan_rejects_unknown_batch(demo_run, capsys):
    assert main(["plan", "b_missing", "--db", str(demo_run.paths.db)]) == 4
    assert "不存在" in capsys.readouterr().err
