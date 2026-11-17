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


def _mode_segment_rows(review: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for segment in review["segments"]:
        if segment["kind"] == "confirmed":
            rows.append([
                "确证", segment["start_ts"], segment["end_ts"],
                f"{segment['state']['raw_value']} / {segment['state']['label'] or '-'}",
                f"{segment['start_line']}–{segment['end_line']}",
                "-", segment["evidence_grade"], segment["detail"],
            ])
        else:
            rows.append([
                "未知", segment["start_ts"], segment["end_ts"], "-",
                ",".join(str(number) for number in segment["line_numbers"]) or "-",
                ",".join(segment["reasons"]), segment["evidence_grade"],
                segment["detail"],
            ])
    return rows


def _mode_pair_rows(review: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for pair in review["transitions"]:
        path = pair["assumed_path"]
        path_text = (
            "→".join(str(state) for state in path) if path else "-"
        )
        windows = "; ".join(
            f"{window['state']}@{window['earliest_ts']}~{window['latest_ts']}"
            for window in pair["plausible_windows"]
        ) or "-"
        rows.append([
            pair["from_ts"], pair["to_ts"],
            f"{pair['from_state']['raw_value']}→{pair['to_state']['raw_value']}",
            f"L{pair['from_line']}→L{pair['to_line']}",
            pair["elapsed_seconds"],
            pair["min_required_seconds"] if pair["min_required_seconds"] is not None else "-",
            path_text, windows, _yes(pair["via_reset"]),
            pair["grade"], pair["verdict"], pair["recommendation"] or "-",
        ])
    return rows


def render_mode_state_markdown(summary: dict[str, Any]) -> list[str]:
    reviews = summary.get("mode_state_review") or []
    if not reviews:
        return []
    lines = ["", "## 枚举状态时序复核", ""]
    lines.append(
        "状态结论仅引用 CRC、解码、量程、枚举全部合格且时间戳可解析的已完成批次读数；"
        "原始 reading 与 issue 不做任何修改。"
    )
    for review in reviews:
        lines.append("")
        lines.append(
            f"### {review['device_id']} / {review['point_key']} "
            f"（核验语义: {review['verification']}，最大未知间隔: "
            f"{review['max_gap_seconds']}s）"
        )
        craw = review["summary"]
        lines.append("")
        lines.append(_table(["直接合法", "未证成", "明确不可能", "未知段", "确证段"], [[
            craw["direct"], craw["unproven"], craw["impossible"],
            craw["unknown_segments"], craw["confirmed_segments"],
        ]]))
        reset = review["reset_state"]
        lines.append("")
        lines.append(
            "复位状态: "
            + (f"{reset['raw_value']} / {reset['label'] or '-'}" if reset else "未声明")
            + ("；输入行时间顺序与时间戳不一致（已按时间戳排序）。"
               if review["out_of_order_input"] else "")
        )
        lines += ["", "#### 逐段证据", ""]
        segment_rows = _mode_segment_rows(review)
        if segment_rows:
            lines.append(_table(
                ["段类型", "起始", "结束", "状态", "行号", "原因", "证据等级", "说明"],
                segment_rows,
            ))
        else:
            lines.append("没有可复核的时间段。")
        lines += ["", "#### 相邻确证观测的证据结论", ""]
        pair_rows = _mode_pair_rows(review)
        if pair_rows:
            lines.append(_table(
                ["起", "止", "状态跳转", "行号", "间隔(s)", "最短需求(s)",
                 "最短假设路径", "中间状态可行窗口", "经复位", "证据等级", "结论", "建议"],
                pair_rows,
            ))
        else:
            lines.append("确证观测不足一对，无法产生跳转结论。")
    lines.append("")
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
    lines += render_mode_state_markdown(summary)
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
