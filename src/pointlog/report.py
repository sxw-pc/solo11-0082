"""Deterministic JSON, Markdown, HTML and CSV report generation."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .contract import ContractSet
from .derived import DerivedConfig
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
    lines.append(render_derived_markdown(summary))
    lines.append("")
    return "\n".join(lines)


def render_derived_markdown(summary: dict[str, Any]) -> str:
    derived = summary.get("derived")
    if derived is None:
        return ""
    lines = [
        "",
        "## 虚拟测点（离线派生）",
        "",
        f"- 表达式版本：`{derived['expression_version']}`",
        f"- 授权误差语义：`{derived['error_semantics']}`",
        f"- 语义说明：{derived['semantics_statement']}",
    ]
    if derived["independent_has_no_guarantee"]:
        lines.append(
            "- 注意：独立随机误差给出的是一阶 1σ 估计范围，**不保证**真值必然落在区间内，"
            "不得与保证界限混称为同一精度。"
        )
    lines += [
        f"- 可判读数：{derived['good_rows']}；不可判读数：{derived['indeterminate_rows']}",
        "",
        _table(
            ["设备", "虚拟点位", "单位", "表达式", "误差语义", "可判", "不可判"],
            [[item["device_id"], item["point_key"], item["unit"] or "-",
              item["expression"], item["error_semantics"], item["good"],
              item["indeterminate"]] for item in derived["points"]],
        ),
        "",
        "### 虚拟读数与误差范围",
        "",
    ]
    reading_rows = []
    for row in derived["readings"]:
        if row["quality_code"] != "derived_good":
            continue
        reading_rows.append([
            row["device_id"], row["point_key"], row["ts"],
            f"{row['engineering_value']:.9g}", row["unit"],
            f"{row['lower_bound']:.9g}", f"{row['upper_bound']:.9g}",
            row["bound_sigma"] or "guaranteed",
            row["threshold_state"] or "-",
        ])
    if reading_rows:
        lines.append(_table(
            ["设备", "点位", "时刻", "数值", "单位", "下界", "上界", "界限语义", "阈值状态"],
            reading_rows,
        ))
    else:
        lines.append("无可判虚拟读数。")
    lines += ["", "### 不可判虚拟读数及原因", ""]
    if derived["indeterminate"]:
        lines.append(_table(
            ["设备", "点位", "时刻", "原因码", "来源行号"],
            [[row["device_id"], row["point_key"], row["ts"], row["reason_codes"],
              row["line_no_json"]] for row in derived["indeterminate"]],
        ))
    else:
        lines.append("无不可判虚拟读数。")
    if derived["thresholds"]:
        lines += ["", "### 阈值判定", "", _table(
            ["点位", "时刻", "语义", "数值", "下界", "上界", "阈值", "判定"],
            [[row["point_key"], row["ts"], row["error_semantics"],
              f"{row['engineering_value']:.9g}" if row["engineering_value"] is not None else "-",
              f"{row['lower_bound']:.9g}" if row["lower_bound"] is not None else "-",
              f"{row['upper_bound']:.9g}" if row["upper_bound"] is not None else "-",
              f"{row['threshold']:.9g}", row["threshold_state"]]
             for row in derived["thresholds"]],
        )]
    lines.append("")
    return "\n".join(lines)


def render_derived_csv(summary: dict[str, Any]) -> str:
    derived = summary.get("derived")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "device_id", "point_key", "ts", "quality_code", "error_semantics",
        "engineering_value", "unit", "lower_bound", "upper_bound", "bound_sigma",
        "threshold", "threshold_state", "reason_codes", "sources_json",
        "expression_version", "line_no_json",
    ])
    if derived is None:
        return output.getvalue()
    for row in derived["readings"]:
        writer.writerow([
            row["device_id"], row["point_key"], row["ts"], row["quality_code"],
            row["error_semantics"],
            "" if row["engineering_value"] is None else row["engineering_value"],
            row["unit"],
            "" if row["lower_bound"] is None else row["lower_bound"],
            "" if row["upper_bound"] is None else row["upper_bound"],
            row["bound_sigma"],
            "" if row["threshold"] is None else row["threshold"],
            row["threshold_state"], row["reason_codes"], row["sources_json"],
            row["expression_version"], row["line_no_json"],
        ])
    return output.getvalue()


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
    derived_config: DerivedConfig | None = None,
) -> dict[str, str]:
    summary = collect_summary(store, batch_id, contract, derived_config)
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
    if summary.get("derived") is not None:
        paths["derived_csv"] = atomic_write_text(
            target / "derived_readings.csv", render_derived_csv(summary)
        )
    return {key: str(path) for key, path in paths.items()}
