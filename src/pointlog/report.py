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
    lines += render_interlock_markdown(summary["interlock"],
                                       summary["interlock_pending_recollect"])
    lines.append("")
    return "\n".join(lines)


def render_interlock_markdown(
    interlock: dict[str, Any], unknown_pending: list[dict[str, Any]]
) -> list[str]:
    """Three-valued interlock section: proven / normal / unknown are distinct."""
    lines = ["", "## 双点位持续联锁", ""]
    rules = interlock["rules"]
    if not rules:
        lines.append("本批次契约未声明双点位联锁规则。")
        return lines
    lines.append(_table(
        ["规则", "设备", "采样语义", "阈值", "最短持续(min)", "陈旧上限(min)",
         "已证联锁(s)", "已证正常(s)", "未知(s)", "事件"],
        [[
            f"{item['rule_id']}@v{item['version']}", item["device_id"],
            item["sampling"],
            " AND ".join(
                f"{point['point_key']} "
                f"{'>' if point['direction'] == 'above' else '<'}"
                f"{'=' if point['inclusive'] else ''} {point['threshold']:g}"
                for point in item["points"]
            ),
            item["min_duration_minutes"], item["max_stale_minutes"],
            item["totals"]["proven_interlock_seconds"],
            item["totals"]["proven_normal_seconds"],
            item["totals"]["unknown_seconds"],
            item["totals"]["event_count"],
        ] for item in rules],
    ))
    event_rows = [
        [
            item["rule_id"], f"{item['start_ts']} — {item['end_ts']}",
            item["duration_seconds"], item["peak_a"], item["peak_b"],
            ",".join(str(number) for number in item["line_numbers"]) or "-",
            item["interruption_reason"],
            ",".join(str(number) for number in item["interruption_lines"]) or "-",
        ]
        for rule in rules for item in rule["events"]
    ]
    lines += ["", "### 已证联锁事件", ""]
    lines.append(_table(
        ["规则", "起止", "持续(s)", "峰值 A", "峰值 B", "源行号",
         "中断原因", "中断行号"],
        event_rows,
    ) if event_rows else "无达到最短持续时间的联锁事件。")
    unknown_rows = [
        [item["rule_id"], f"{item['start_ts']} — {item['end_ts']}",
         item["duration_seconds"], item["reason"],
         ",".join(str(number) for number in item["line_numbers"]) or "-"]
        for rule in rules for item in rule["unknown_intervals"]
    ]
    lines += ["", "### 未知缺口（不计为正常）", ""]
    lines.append(_table(
        ["规则", "起止", "持续(s)", "原因", "源行号"], unknown_rows,
    ) if unknown_rows else "无未知缺口。")
    if unknown_pending:
        lines += ["", "### 联锁未知待复采", ""]
        lines.append(_table(
            ["设备", "点位", "规则", "起止", "原因", "行号"],
            [[item["device_id"], item["point_key"], item["rule_id"],
              f"{item['start_ts']} — {item['end_ts']}", item["reason"],
              ",".join(str(number) for number in item["line_numbers"]) or "-"]
             for item in unknown_pending],
        ))
    return lines


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


def render_interlock_pending_csv(summary: dict[str, Any]) -> str:
    """Unknown interlock spans, kept separate from single-point recollection."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "device_id", "point_key", "rule_id", "rule_version", "issue_code",
        "state", "start_ts", "end_ts", "reason", "line_numbers", "detail",
    ])
    for item in summary["interlock_pending_recollect"]:
        writer.writerow([
            item["device_id"],
            item["point_key"],
            item["rule_id"],
            item["rule_version"],
            item["issue_code"],
            item["state"],
            item["start_ts"],
            item["end_ts"],
            item["reason"],
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
        "interlock_csv": atomic_write_text(
            target / "interlock_pending.csv",
            render_interlock_pending_csv(summary),
        ),
    }
    return {key: str(path) for key, path in paths.items()}
