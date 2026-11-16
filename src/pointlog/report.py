"""Deterministic JSON, Markdown, HTML and CSV report generation."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .contract import ContractSet
from .quality import quality_meta
from .report_html import render_html
from .report_stats import collect_summary
from .store import Store
from .util import atomic_write_text


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines)


def _yes(value: bool) -> str:
    return "是" if value else "否"


def _render_recollect_plan(plan: dict[str, Any]) -> list[str]:
    usage = plan["usage"]
    budget = plan["budget"]
    window = budget["window_seconds"]
    lines = [
        "",
        "## 重采计划",
        "",
        "> 离线规划：未连接设备、未发送任何帧；任一请求失败会同时影响其覆盖的全部点位。",
        "",
        _table(["指标", "值"], [
            ["规划目标", plan["objective"]],
            ["请求预算（已用/上限）", f"{usage['requests_used']}/{budget['max_requests']}"],
            ["独立失败机会", usage["independent_failure_chances"]],
            ["覆盖点位（已覆盖/待重采）", f"{usage['covered_points']}/{usage['pending_points']}"],
            ["窗口用量（秒，已用/上限）",
             f"{usage['window_used_s']} / {window if window is not None else '-'}"],
            ["每设备冷却（秒）", budget["device_cooldown_s"]],
        ]),
        "",
        "### 计划请求",
        "",
    ]
    if plan["requests"]:
        lines.append(_table(
            ["请求", "从站", "功能", "起始", "数量", "覆盖点位", "附带点位",
             "计划时刻(秒)", "请求帧(hex)"],
            [[request["id"], request["slave_id"], request["function"],
              request["start"], request["count"],
              ", ".join(f"{item['device_id']}/{item['point_key']}"
                        for item in request["covers"]),
              ", ".join(request["incidental_points"]) or "-",
              f"{request['scheduled_start_s']}–{request['scheduled_end_s']}",
              request["request_hex"]] for request in plan["requests"]],
        ))
    else:
        lines.append("无可行请求。")
    lines += ["", "### 未覆盖点位", ""]
    uncovered = [point for point in plan["points"] if point["status"] == "uncovered"]
    if uncovered:
        lines.append(_table(
            ["设备", "点位", "阻断原因", "说明"],
            [[point["device_id"] or "-", point["point_key"] or "-",
              (point["block"] or {}).get("code", ""),
              (point["block"] or {}).get("detail", "")] for point in uncovered],
        ))
    else:
        lines.append("全部待重采点位均已覆盖。")
    return lines


def render_markdown(summary: dict[str, Any]) -> str:
    batch = summary["batch"]
    totals = summary["totals"]
    quality = summary["quality_code_distribution"]
    issues = summary["issue_distribution"]
    lines = [
        "# 点位采录台采集报告",
        "",
        "## 批次信息",
        "",
        _table(["字段", "值"], [
            ["批次 ID", batch["id"]],
            ["状态", batch["status"]],
            ["开始时间", batch["started_at"]],
            ["结束时间", batch["finished_at"] or "-"],
            ["回放文件", batch["replay_file"]],
            ["回放 SHA-256", batch["replay_sha256"]],
            ["点位契约", batch["contract_file"]],
            ["契约 SHA-256", batch["contract_sha256"]],
            ["回放总行数", batch["total_lines"]],
            ["已处理行数", batch["processed_lines"]],
        ]),
        "",
        "## 概览",
        "",
        _table(["指标", "数量"], [
            ["帧记录", totals["frames"]],
            ["读数记录", totals["readings"]],
            ["有效读数（good）", totals["valid_readings"]],
            ["问题记录", totals["issues"]],
            ["待重采点位", totals["pending_points"]],
        ]),
        "",
        "## 质量码分布",
        "",
    ]
    if quality:
        rows = []
        for code, count in quality.items():
            meta = quality_meta(code)
            rows.append([code, meta["severity"], _yes(bool(meta["pending_recollect"])), count])
        lines.append(_table(["质量码", "严重级别", "进入待重采", "数量"], rows))
    else:
        lines.append("无读数或质量事件。")
    lines += ["", "## 问题分布", ""]
    if issues:
        lines.append(_table(
            ["问题码", "数量"], [[code, count] for code, count in issues.items()]
        ))
    else:
        lines.append("无问题记录。")
    lines += ["", "## 按设备统计", ""]
    lines.append(_table(
        ["设备", "读数", "good", "问题", "待重采"],
        [[item["device_id"], item["reading_count"], item["good_count"],
          item["issue_count"], item["pending_count"]] for item in summary["devices"]],
    ))
    lines += ["", "## 按点位统计", ""]
    lines.append(_table(
        ["设备", "点位", "单位", "读数", "good", "问题", "待重采"],
        [[item["device_id"], item["point_key"], item["unit"] or "-",
          item["reading_count"], item["good_count"], item["issue_count"],
          _yes(item["pending"])] for item in summary["points"]],
    ))
    lines += ["", "## 待重采点位", ""]
    pending = summary["pending_recollect"]
    if pending:
        lines.append(_table(
            ["设备", "点位", "问题码", "质量码", "次数", "行号", "说明"],
            [[item["device_id"], item["point_key"], item["issue_code"],
              item["quality_code"], item["occurrences"],
              ",".join(str(number) for number in item["line_numbers"]) or "-",
              item["detail"]] for item in pending],
        ))
    else:
        lines.append("无待重采点位。")
    plan = summary.get("recollect_plan")
    if plan is not None:
        lines += _render_recollect_plan(plan)
    lines.append("")
    return "\n".join(lines)


def render_pending_csv(summary: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "device_id", "point_key", "issue_code", "quality_code", "severity",
        "occurrences", "line_numbers", "detail",
    ])
    for item in summary["pending_recollect"]:
        writer.writerow([
            item["device_id"],
            item["point_key"],
            item["issue_code"],
            item["quality_code"],
            item["severity"],
            item["occurrences"],
            ";".join(str(number) for number in item["line_numbers"]),
            item["detail"],
        ])
    return output.getvalue()


def write_reports(
    store: Store,
    batch_id: str,
    contract: ContractSet,
    reports_dir: str | Path,
) -> dict[str, str]:
    summary = collect_summary(store, batch_id, contract)
    target = Path(reports_dir)
    target.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": atomic_write_text(
            target / "summary.json",
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        ),
        "markdown": atomic_write_text(
            target / "summary.md", render_markdown(summary)
        ),
        "html": atomic_write_text(
            target / "summary.html", render_html(summary)
        ),
        "csv": atomic_write_text(
            target / "pending_recollect.csv", render_pending_csv(summary)
        ),
    }
    return {key: str(path) for key, path in paths.items()}
