"""Read-only local review console bound exclusively to the loopback interface."""

from __future__ import annotations

import re
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .quality import quality_meta
from .store import Store
from .store_queries import (
    batch_list,
    batch_totals,
    issue_distribution,
    issue_rows,
    pending_recollect,
    problem_frames,
    quality_distribution,
)

LOOPBACK_HOST = "127.0.0.1"
BATCH_PATH = re.compile(r"^/batch/([A-Za-z0-9_.-]+)$")
CSS = """
body { margin:0; background:#f5f7f9; color:#18222d;
       font:14px/1.5 "Segoe UI","Microsoft YaHei",sans-serif; }
main { max-width:1180px; margin:0 auto; padding:24px 18px 48px; }
h1 { font-size:25px; margin:0 0 5px; }
h2 { font-size:18px; color:#1f5f8b; margin:26px 0 9px; }
p.lead { color:#61707f; margin:0 0 18px; }
a { color:#1f5f8b; }
table { width:100%; border-collapse:collapse; background:#fff;
        border:1px solid #d6dee5; margin:8px 0; }
th,td { border-bottom:1px solid #e2e8ed; padding:7px 9px; text-align:left;
        vertical-align:top; }
th { background:#edf2f6; }
tr:last-child td { border-bottom:0; }
code,.mono { font-family:Consolas,"Courier New",monospace; font-size:12px;
             overflow-wrap:anywhere; }
.cards { display:flex; flex-wrap:wrap; gap:10px; margin:12px 0; }
.card { background:#fff; border:1px solid #d6dee5; border-radius:7px;
        padding:10px 13px; min-width:130px; }
.card b { display:block; font-size:21px; margin-top:2px; }
.note { background:#fff8e6; border:1px solid #e7d59a; padding:9px 11px;
        border-radius:6px; }
.bad { color:#982b2b; font-weight:600; }
.warn { color:#855600; font-weight:600; }
.good { color:#1f5f8b; font-weight:600; }
"""


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _layout(title: str, body: str) -> bytes:
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title><style>{CSS}</style></head>
<body><main>{body}</main></body></html>"""
    return html.encode("utf-8")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    if not rows:
        return (
            f"<table><thead><tr>{head}</tr></thead>"
            f'<tbody><tr><td colspan="{len(headers)}">无记录</td></tr></tbody></table>'
        )
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _severity_cell(code: str) -> str:
    level = str(quality_meta(code)["severity"])
    css = {"error": "bad", "warning": "warn"}.get(level, "good")
    return f'<span class="{css}">{_e(level)}</span>'


def _index_page(store: Store) -> bytes:
    rows = []
    for batch in batch_list(store):
        totals = batch_totals(store, batch["id"])
        rows.append([
            f'<a href="/batch/{_e(batch["id"])}"><code>{_e(batch["id"])}</code></a>',
            _e(batch["status"]),
            _e(batch["started_at"]),
            _e(totals["frames"]),
            _e(totals["valid_readings"]),
            _e(totals["issues"]),
        ])
    body = (
        "<h1>点位采录台 · 只读复核台</h1>"
        '<p class="lead">仅绑定 127.0.0.1，只提供 GET 读取，不提供写入、删除或上传接口。</p>'
        + _table(["批次", "状态", "开始时间", "帧", "有效读数", "问题"], rows)
    )
    return _layout("点位采录台 · 批次列表", body)


def _batch_page(store: Store, batch: dict[str, Any]) -> bytes:
    batch_id = batch["id"]
    totals = batch_totals(store, batch_id)
    quality = quality_distribution(store, batch_id)
    issues = issue_distribution(store, batch_id)
    pending = pending_recollect(store, batch_id)
    quality_rows = [
        [f"<code>{_e(code)}</code>", _severity_cell(code), _e(count)]
        for code, count in quality.items()
    ]
    issue_rows_html = [
        [_e(code), _e(count)] for code, count in issues.items()
    ]
    pending_rows = [
        [f"<code>{_e(item['device_id'])}</code>",
         f"<code>{_e(item['point_key'])}</code>",
         _e(item["issue_code"]), _e(item["quality_code"]),
         _e(item["occurrences"]),
         _e(",".join(str(number) for number in item["line_numbers"]) or "-")]
        for item in pending
    ]
    issue_detail_rows = []
    for row in issue_rows(store, batch_id):
        issue_detail_rows.append([
            _e(row["line_no"]), f"<code>{_e(row['device_id'])}</code>",
            f"<code>{_e(row['point_key'])}</code>", _e(row["issue_code"]),
            _e(row["quality_code"]), _e(row["severity"]),
            f"<code>{_e(row['frame_summary'])}</code>", _e(row["detail"]),
        ])
    frame_rows = []
    for row in problem_frames(store, batch_id):
        frame_rows.append([
            _e(row["line_no"]), _e(row["quality_code"]), _e(row["issue_code"] or "-"),
            f"<code>{_e(row['request_hex'][:48])}</code>",
            f"<code>{_e(row['response_hex'][:48])}</code>",
            _e(row["detail"]),
        ])
    body = f"""
<p><a href="/">← 返回批次列表</a></p>
<h1>批次详情</h1>
<p class="lead mono">{_e(batch_id)}</p>
<div class="cards">
<div class="card">状态<b>{_e(batch['status'])}</b></div>
<div class="card">帧<b>{_e(totals['frames'])}</b></div>
<div class="card">读数<b>{_e(totals['readings'])}</b></div>
<div class="card">有效读数<b>{_e(totals['valid_readings'])}</b></div>
<div class="card">问题<b>{_e(totals['issues'])}</b></div>
<div class="card">待重采点位<b>{_e(totals['pending_points'])}</b></div>
</div>
{_table(["字段", "值"], [
    ["开始时间", _e(batch["started_at"])],
    ["结束时间", _e(batch["finished_at"] or "-")],
    ["回放文件", _e(Path(batch["replay_path"]).name)],
    ["回放 SHA-256", f'<span class="mono">{_e(batch["replay_sha256"])}</span>'],
    ["契约文件", _e(Path(batch["contract_path"]).name)],
    ["契约 SHA-256", f'<span class="mono">{_e(batch["contract_sha256"])}</span>'],
    ["回放总行数 / 已处理", f"{_e(batch['total_lines'])} / {_e(batch['processed_lines'])}"],
])}
<h2>质量码分布</h2>
{_table(["质量码", "严重级别", "数量"], quality_rows)}
<h2>问题分布</h2>
{_table(["问题码", "数量"], issue_rows_html)}
<h2>待重采点位</h2>
{_table(["设备", "点位", "问题码", "质量码", "次数", "行号"], pending_rows)}
<h2>问题明细</h2>
{_table(["行", "设备", "点位", "问题码", "质量码", "级别", "帧摘要", "说明"], issue_detail_rows)}
<h2>问题帧列表</h2>
{_table(["行", "质量码", "问题码", "请求帧摘要", "应答帧摘要", "说明"], frame_rows)}
"""
    return _layout(f"批次 {batch_id}", body)


def _error_page(status: int, message: str) -> bytes:
    return _layout(
        f"{status} {message}",
        f"<h1>{status} {_e(message)}</h1>"
        '<p><a href="/">返回批次列表</a></p>',
    )


def make_handler(db_path: str | Path):
    database = str(db_path)

    class PointlogHandler(BaseHTTPRequestHandler):
        server_version = "pointlog-readonly/0.1"

        def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if status == 405:
                self.send_header("Allow", "GET")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            try:
                store = Store(database, read_only=True)
            except Exception:
                self._send(500, _error_page(500, "无法打开采集台账"))
                return
            try:
                if self.path == "/":
                    self._send(200, _index_page(store))
                    return
                match = BATCH_PATH.match(self.path)
                if match:
                    batch = store.get_batch(match.group(1))
                    if batch is None:
                        self._send(404, _error_page(404, "批次不存在"))
                    else:
                        self._send(200, _batch_page(store, batch))
                    return
                self._send(404, _error_page(404, "路径不存在"))
            finally:
                store.close()

        def _read_only(self):
            self._send(405, _error_page(405, "只读服务不接收该请求方法"))

        do_POST = _read_only
        do_PUT = _read_only
        do_PATCH = _read_only
        do_DELETE = _read_only
        do_HEAD = _read_only
        do_OPTIONS = _read_only
        do_TRACE = _read_only

        def log_message(self, format, *args):
            return

    return PointlogHandler


def create_server(db_path: str | Path, port: int = 5000) -> HTTPServer:
    database = Path(db_path)
    if not database.is_file():
        raise FileNotFoundError(f"database does not exist: {database}")
    server = HTTPServer((LOOPBACK_HOST, int(port)), make_handler(database))
    server.daemon_threads = True
    return server


def serve(db_path: str | Path, port: int = 5000) -> None:
    server = create_server(db_path, port)
    host, bound_port = server.server_address[:2]
    print(f"只读复核台: http://{host}:{bound_port}/")
    print("仅监听回环地址；按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
