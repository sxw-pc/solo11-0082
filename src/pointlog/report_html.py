"""Self-contained HTML report renderer (no external assets, no scripts)."""

from __future__ import annotations

from html import escape
from typing import Any

from .quality import quality_meta

CSS = """
:root { color-scheme: light; --ink:#17202a; --muted:#5d6d7e; --line:#d5dde5;
        --paper:#f7f9fb; --card:#ffffff; --accent:#1f5f8b; --warn:#8a5a00;
        --bad:#9b2c2c; }
* { box-sizing: border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
       font:15px/1.55 "Segoe UI", "Microsoft YaHei", sans-serif; }
main { max-width:1180px; margin:0 auto; padding:28px 20px 56px; }
header { border-bottom:3px solid var(--accent); padding-bottom:16px; margin-bottom:22px; }
h1 { font-size:27px; margin:0 0 6px; letter-spacing:.02em; }
h2 { font-size:19px; margin:28px 0 10px; color:var(--accent); }
p.lead { color:var(--muted); margin:0; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
        gap:12px; margin:14px 0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
        padding:13px 15px; }
.card .label { color:var(--muted); font-size:12px; text-transform:uppercase;
               letter-spacing:.05em; }
.card .value { font-size:24px; font-weight:650; margin-top:3px; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--line); font-size:14px; }
th, td { border-bottom:1px solid var(--line); padding:8px 10px;
         text-align:left; vertical-align:top; }
th { background:#eef3f7; font-weight:650; }
tr:last-child td { border-bottom:0; }
code, .mono { font-family:Consolas, "Courier New", monospace; font-size:12px;
              overflow-wrap:anywhere; }
.note { background:#fff8e6; border:1px solid #ead9a5; padding:10px 12px;
        border-radius:7px; color:var(--warn); }
.sev-error { color:var(--bad); font-weight:650; }
.sev-warning { color:var(--warn); font-weight:650; }
.sev-info { color:var(--accent); font-weight:650; }
footer { color:var(--muted); border-top:1px solid var(--line); margin-top:30px;
         padding-top:12px; font-size:12px; }
"""


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>")
    if not body:
        body.append(f'<tr><td colspan="{len(headers)}">无记录</td></tr>')
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _metric(label: str, value: Any) -> str:
    return (
        '<div class="card"><div class="label">'
        + _e(label)
        + '</div><div class="value">'
        + _e(value)
        + "</div></div>"
    )


def _severity(value: str) -> str:
    css = {"error": "sev-error", "warning": "sev-warning"}.get(value, "sev-info")
    return f'<span class="{css}">{_e(value)}</span>'


def _stat(value: Any) -> str:
    """Escaped cell for a statistic that may be marked unavailable."""
    return _e("不可用" if value is None else value)


def _pending_flag(value: Any) -> str:
    return _e("不可用" if value is None else ("是" if value else "否"))


def render_html(summary: dict[str, Any]) -> str:
    batch = summary["batch"]
    fence = summary["version_fence"]
    totals = summary["totals"]
    stats_available = totals["stats_status"] == "available"
    quality = summary["quality_code_distribution"]
    quality_rows = []
    for code, count in quality.items():
        meta = quality_meta(code)
        yes = "是" if meta["pending_recollect"] else "否"
        quality_rows.append([
            f"<code>{_e(code)}</code>",
            _severity(str(meta["severity"])),
            _e(yes),
            _e(count),
        ])
    issue_rows = [
        [f"<code>{_e(code)}</code>", _e(count)] for code, count in
        summary["issue_distribution"].items()
    ]
    device_rows = [
        [_e(item["device_id"]), _e(item["reading_count"]), _stat(item["good_count"]),
         _e(item["issue_count"]), _stat(item["pending_count"])]
        for item in summary["devices"]
    ]
    point_rows = [
        [f"<code>{_e(item['device_id'])}</code>",
         f"<code>{_e(item['point_key'])}</code>", _e(item["unit"] or "-"),
         _e(item["reading_count"]), _stat(item["good_count"]),
         _e(item["issue_count"]), _pending_flag(item["pending"])]
        for item in summary["points"]
    ]
    pending = summary["pending_recollect"]
    pending_rows = [
        [f"<code>{_e(item['device_id'])}</code>",
         f"<code>{_e(item['point_key'])}</code>",
         f"<code>{_e(item['issue_code'])}</code>",
         f"<code>{_e(item['quality_code'])}</code>",
         _e(item["occurrences"]),
         _e(",".join(str(number) for number in item["line_numbers"]) or "-"),
         _e(item["detail"])]
        for item in (pending or [])
    ]
    if pending is None:
        pending_note = (
            '<p class="note">版本围栏未通过：待重采清单不可用，帧记录仅供诊断。</p>'
        )
    elif pending_rows:
        pending_note = (
            '<p class="note">存在待重采点位，请按 CSV 清单安排回放补采。</p>'
        )
    else:
        pending_note = '<p class="note">本次采集没有待重采点位。</p>'
    fence_rows = "".join(
        f"<tr><th>{_e(label)}</th><td>{cell}</td></tr>"
        for label, cell in [
            ("围栏路由", _e(fence["route"])),
            ("围栏结果", _e(fence["result"])),
            ("来源路径", f'<span class="mono">{_e(fence["source_path"])}</span>'),
            ("封存副本", f'<span class="mono">{_e(fence["sealed_path"] or "-")}</span>'),
            ("处理版本 SHA-256", f'<span class="mono">{_e(fence["sha256"])}</span>'),
            ("字节数", _e(fence["byte_count"] if fence["byte_count"] is not None else "-")),
            ("行数", _e(fence["total_lines"])),
            ("来源现状", _e(fence["source_status"])),
            ("说明", _e(fence["detail"] or "-")),
        ]
    )
    stats_note = "" if stats_available else (
        '<p class="note">版本围栏未通过：有效读数与待重采统计不可用，'
        "已提交帧仅保留用于诊断。</p>"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>点位采录台采集报告 · {_e(batch['id'])}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<header>
<h1>点位采录台采集报告</h1>
<p class="lead">离线回放采集台账 · 批次 <span class="mono">{_e(batch['id'])}</span></p>
</header>

<section>
<h2>批次信息</h2>
<table><tbody>
<tr><th>状态</th><td>{_e(batch['status'])}</td></tr>
<tr><th>开始时间</th><td>{_e(batch['started_at'])}</td></tr>
<tr><th>结束时间</th><td>{_e(batch['finished_at'] or '-')}</td></tr>
<tr><th>回放文件</th><td>{_e(batch['replay_file'])}</td></tr>
<tr><th>回放 SHA-256</th><td class="mono">{_e(batch['replay_sha256'])}</td></tr>
<tr><th>点位契约</th><td>{_e(batch['contract_file'])}</td></tr>
<tr><th>契约 SHA-256</th><td class="mono">{_e(batch['contract_sha256'])}</td></tr>
<tr><th>回放总行数 / 已处理</th><td>{_e(batch['total_lines'])} / {_e(batch['processed_lines'])}</td></tr>
</tbody></table>
</section>

<section>
<h2>版本围栏</h2>
<table><tbody>
{fence_rows}
</tbody></table>
{stats_note}
</section>

<section>
<h2>概览</h2>
<div class="grid">
{_metric('帧记录', totals['frames'])}
{_metric('读数记录', totals['readings'])}
{_metric('有效读数 good', '不可用' if totals['valid_readings'] is None else totals['valid_readings'])}
{_metric('问题记录', totals['issues'])}
{_metric('待重采点位', '不可用' if totals['pending_points'] is None else totals['pending_points'])}
</div>
</section>

<section>
<h2>质量码分布</h2>
{_table(['质量码', '严重级别', '进入待重采', '数量'], quality_rows)}
</section>

<section>
<h2>问题分布</h2>
{_table(['问题码', '数量'], issue_rows)}
</section>

<section>
<h2>按设备统计</h2>
{_table(['设备', '读数', 'good', '问题', '待重采'], device_rows)}
</section>

<section>
<h2>按点位统计</h2>
{_table(['设备', '点位', '单位', '读数', 'good', '问题', '待重采'], point_rows)}
</section>

<section>
<h2>待重采点位</h2>
{pending_note}
{_table(['设备', '点位', '问题码', '质量码', '次数', '行号', '说明'], pending_rows)}
</section>

<footer>
本报告由 pointlog 离线生成。页面样式全部内联，不加载外部脚本、字体或资源。
所有帧、设备名、点位与时间戳均为合成 fixture。
</footer>
</main>
</body>
</html>
"""
