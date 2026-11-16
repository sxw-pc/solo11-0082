import csv
import json

from pointlog.pipeline import run_capture


def test_json_summary_has_stable_shape(demo_run):
    summary = json.loads((demo_run.paths.reports / "summary.json").read_text(encoding="utf-8"))
    assert list(summary) == [
        "schema_version",
        "batch",
        "totals",
        "quality_code_distribution",
        "issue_distribution",
        "devices",
        "points",
        "pending_recollect",
        "interlock",
        "interlock_pending_recollect",
    ]
    assert list(summary["batch"]) == [
        "id", "status", "started_at", "finished_at", "total_lines",
        "processed_lines", "replay_file", "replay_sha256", "contract_file",
        "contract_sha256",
    ]
    assert summary["schema_version"] == 2
    assert summary["batch"]["status"] == "completed"
    assert summary["batch"]["total_lines"] == 19
    assert summary["batch"]["replay_file"] == "replay_demo.jsonl"
    assert summary["totals"] == {
        "frames": 19,
        "readings": 11,
        "valid_readings": 8,
        "issues": 12,
        "pending_points": 7,
    }
    assert summary["quality_code_distribution"]["good"] == 8
    assert summary["quality_code_distribution"]["bad_frame"] == 3
    assert summary["issue_distribution"]["byte_count_mismatch"] == 1
    assert len(summary["devices"]) == 2
    assert len(summary["points"]) == 9
    assert len(summary["pending_recollect"]) == 7


def test_markdown_golden_fragments(demo_run):
    text = (demo_run.paths.reports / "summary.md").read_text(encoding="utf-8")
    assert text.startswith("# 点位采录台采集报告\n")
    for heading in ("## 批次信息", "## 概览", "## 质量码分布",
                    "## 问题分布", "## 按设备统计", "## 按点位统计",
                    "## 待重采点位"):
        assert heading in text
    assert "| good | info | 否 | 8 |" in text
    assert "| bad_frame | error | 是 | 3 |" in text
    assert "| missing_response | warning | 是 | 1 |" in text
    assert "| line_a_energy | missing_response | missing_response | 1 | - |" in text
    assert "| demo-gateway | line_a_speed | slave_mismatch | bad_frame | 1 | 18 |" in text


def test_html_is_self_contained(demo_run):
    text = (demo_run.paths.reports / "summary.html").read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    assert "<style>" in text
    assert "质量码分布" in text
    assert "待重采点位" in text
    assert "bad_crc_request" in text
    assert "http://" not in text
    assert "https://" not in text
    assert "<script" not in text
    assert "cdn" not in text.lower()


def test_pending_csv(demo_run):
    path = demo_run.paths.reports / "pending_recollect.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == [
        "device_id", "point_key", "issue_code", "quality_code", "severity",
        "occurrences", "line_numbers", "detail",
    ]
    assert len(rows) == 7
    assert rows[0]["point_key"] == "line_a_flow"
    assert rows[0]["issue_code"] == "exception_response"
    energy = [row for row in rows if row["point_key"] == "line_a_energy"]
    assert energy and energy[0]["line_numbers"] == ""


def test_reports_are_deterministic_across_rerun(demo_run):
    names = ("summary.json", "summary.md", "summary.html",
             "pending_recollect.csv", "interlock_pending.csv")
    before = {
        name: (demo_run.paths.reports / name).read_bytes() for name in names
    }
    run_capture(
        replay_path=demo_run.paths.fixture,
        contract_path=demo_run.paths.contract,
        db_path=demo_run.paths.db,
        reports_dir=demo_run.paths.reports,
    )
    after = {
        name: (demo_run.paths.reports / name).read_bytes() for name in names
    }
    assert after == before
