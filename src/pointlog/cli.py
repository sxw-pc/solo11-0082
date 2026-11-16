"""Command-line interface for pointlog."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from .contract import ContractError, load_contract
from .fixtures_gen import generate_demo_replay
from .pipeline import IngestInterrupted, PipelineError, RunResult, run_capture
from .recollect_plan import build_recollect_plan
from .replay import ReplayError
from .resume import ResumeRejected
from .server import serve
from .store import Store
from .store_queries import (
    batch_list,
    batch_totals,
    issue_distribution,
    pending_recollect,
    quality_distribution,
)
from .util import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    DEFAULT_FIXTURE_PATH,
    DEFAULT_REPORTS_DIR,
)


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite 台账路径")


def _print_result(result: RunResult) -> None:
    flags = []
    if result.reused:
        flags.append("复用已完成批次")
    if result.resumed:
        flags.append("断点续采")
    suffix = f" ({', '.join(flags)})" if flags else ""
    print(f"批次:         {result.batch_id}{suffix}")
    print(f"状态:         {result.status}")
    print(f"处理行数:     {result.processed_lines}/{result.total_lines}")
    print(f"读数记录:     {result.reading_count}")
    print(f"有效读数:     {result.valid_readings}")
    print(f"问题记录:     {result.issue_count}")
    print(f"待重采点位:   {result.pending_points}")
    for name, path in result.report_paths.items():
        print(f"报告[{name}]: {path}")


def _run_capture_command(args: argparse.Namespace) -> int:
    result = run_capture(
        replay_path=args.replay,
        contract_path=args.contract,
        db_path=args.db,
        reports_dir=None if args.no_report else args.reports,
        resume=args.resume,
        stop_after=args.stop_after,
    )
    _print_result(result)
    return 0


def _demo_command(args: argparse.Namespace) -> int:
    print("生成确定性合成 fixture ...")
    fixture = generate_demo_replay(args.contract, args.fixture)
    print(f"fixture: {fixture['path']} ({fixture['lines']} lines)")
    result = run_capture(
        replay_path=args.fixture,
        contract_path=args.contract,
        db_path=args.db,
        reports_dir=args.reports,
    )
    _print_result(result)
    return 0


def _fixtures_command(args: argparse.Namespace) -> int:
    result = generate_demo_replay(args.contract, args.output)
    print(f"fixture: {result['path']}")
    print(f"lines:   {result['lines']}")
    print(f"sha256:  {result['sha256']}")
    return 0


def _open_store(path: str) -> Store:
    if not Path(path).is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    return Store(path, read_only=True)


def _batches_command(args: argparse.Namespace) -> int:
    store = _open_store(args.db)
    try:
        rows = batch_list(store, limit=args.limit)
        if not rows:
            print("没有批次。")
            return 0
        print(f"{'batch_id':34} {'status':10} {'reads':>6} {'good':>6} {'issues':>6}  started_at")
        for batch in rows:
            totals = batch_totals(store, batch["id"])
            print(
                f"{batch['id']:34} {batch['status']:10} "
                f"{totals['readings']:6d} {totals['valid_readings']:6d} "
                f"{totals['issues']:6d}  {batch['started_at']}"
            )
    finally:
        store.close()
    return 0


def _show_command(args: argparse.Namespace) -> int:
    store = _open_store(args.db)
    try:
        batch = store.get_batch(args.batch_id)
        if batch is None:
            print(f"批次不存在: {args.batch_id}", file=sys.stderr)
            return 4
        totals = batch_totals(store, batch["id"])
        print(f"批次:       {batch['id']}")
        print(f"状态:       {batch['status']}")
        print(f"回放文件:   {Path(batch['replay_path']).name}")
        print(f"回放 SHA:   {batch['replay_sha256']}")
        print(f"契约 SHA:   {batch['contract_sha256']}")
        print(f"行数:       {batch['processed_lines']}/{batch['total_lines']}")
        print(f"读数/有效:  {totals['readings']}/{totals['valid_readings']}")
        print(f"问题/待重采点位: {totals['issues']}/{totals['pending_points']}")
        print("质量码分布:")
        for code, count in quality_distribution(store, batch["id"]).items():
            print(f"  {code:20} {count}")
        print("问题码分布:")
        for code, count in issue_distribution(store, batch["id"]).items():
            print(f"  {code:20} {count}")
        pending = pending_recollect(store, batch["id"])
        print(f"待重采清单: {len(pending)} 类")
        for item in pending:
            print(f"  {item['device_id']}/{item['point_key']}  {item['issue_code']}  x{item['occurrences']}")
    finally:
        store.close()
    return 0


def _serve_command(args: argparse.Namespace) -> int:
    serve(args.db, args.port)
    return 0


def _print_plan(plan: dict) -> None:
    usage = plan["usage"]
    budget = plan["budget"]
    window = budget["window_seconds"]
    print("重采计划（离线规划：未连接设备、未发送任何帧）")
    print(f"规划目标:     {plan['objective']}")
    print(f"请求预算:     {usage['requests_used']}/{budget['max_requests']}")
    window_text = "-" if window is None else f"{usage['window_used_s']}s / {window}s"
    print(f"窗口用量:     {window_text}（每设备冷却 {budget['device_cooldown_s']}s）")
    print(f"覆盖点位:     {usage['covered_points']}/{usage['pending_points']}")
    print(f"独立失败机会: {usage['independent_failure_chances']}")
    if plan["requests"]:
        print("计划请求:")
        for request in plan["requests"]:
            covers = ", ".join(
                f"{item['device_id']}/{item['point_key']}" for item in request["covers"]
            )
            print(
                f"  {request['id']}  slave={request['slave_id']} "
                f"func={request['function']} start={request['start']} "
                f"count={request['count']}  覆盖: {covers}  "
                f"帧: {request['request_hex']}"
            )
    uncovered = [point for point in plan["points"] if point["status"] == "uncovered"]
    if uncovered:
        print("未覆盖点位:")
        for point in uncovered:
            block = point["block"] or {}
            name = f"{point['device_id']}/{point['point_key']}"
            print(f"  {name}  {block.get('code', '')}  {block.get('detail', '')}")


def _plan_command(args: argparse.Namespace) -> int:
    if args.max_requests is not None and args.max_requests < 0:
        print("--max-requests 必须是非负整数", file=sys.stderr)
        return 2
    store = _open_store(args.db)
    try:
        batch = store.get_batch(args.batch_id)
        if batch is None:
            print(f"批次不存在: {args.batch_id}", file=sys.stderr)
            return 4
        if batch["status"] != "completed":
            print(
                f"批次未完成: {args.batch_id} 状态为 {batch['status']}，"
                "重采计划只针对已完成批次",
                file=sys.stderr,
            )
            return 4
        contract = load_contract(args.contract or batch["contract_path"])
        if contract.recollect is None:
            print("契约未启用重采规划：缺少顶层 recollect 段", file=sys.stderr)
            return 2
        plan = build_recollect_plan(
            pending_recollect(store, batch["id"]),
            contract,
            contract.recollect,
            base_time=batch["finished_at"] or batch["started_at"],
            objective=args.objective,
            max_requests=args.max_requests,
        )
    finally:
        store.close()
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        _print_plan(plan)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pointlog", description="点位采录台 · 离线寄存器帧回放采集"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="生成 fixture 并跑通一次采集")
    demo.add_argument("--contract", default=str(DEFAULT_CONFIG_PATH))
    demo.add_argument("--fixture", default=str(DEFAULT_FIXTURE_PATH))
    demo.add_argument("--db", default=str(DEFAULT_DB_PATH))
    demo.add_argument("--reports", default=str(DEFAULT_REPORTS_DIR))
    demo.set_defaults(func=_demo_command)

    run = sub.add_parser("run", help="执行一次回放采集")
    run.add_argument("--replay", default=str(DEFAULT_FIXTURE_PATH))
    run.add_argument("--contract", default=str(DEFAULT_CONFIG_PATH))
    _add_db(run)
    run.add_argument("--reports", default=str(DEFAULT_REPORTS_DIR))
    run.add_argument("--no-report", action="store_true", help="不生成报告文件")
    run.add_argument("--resume", action="store_true", help="从已记录的行偏移断点续采")
    run.add_argument("--stop-after", type=int, default=None,
                     help="调试用：处理 N 行后模拟中断")
    run.set_defaults(func=_run_capture_command)

    fixtures = sub.add_parser("fixtures", help="确定性重新生成 fixture")
    fixtures.add_argument("--contract", default=str(DEFAULT_CONFIG_PATH))
    fixtures.add_argument("--output", default=str(DEFAULT_FIXTURE_PATH))
    fixtures.set_defaults(func=_fixtures_command)

    batches = sub.add_parser("batches", help="列出批次摘要")
    _add_db(batches)
    batches.add_argument("--limit", type=int, default=50)
    batches.set_defaults(func=_batches_command)

    show = sub.add_parser("show", help="查看指定批次摘要")
    _add_db(show)
    show.add_argument("batch_id")
    show.set_defaults(func=_show_command)

    plan = sub.add_parser("plan", help="为已完成批次生成离线重采计划（不连接设备）")
    _add_db(plan)
    plan.add_argument("batch_id")
    plan.add_argument("--contract", default=None,
                      help="点位契约路径（默认使用批次记录的契约）")
    plan.add_argument("--objective", choices=["priority", "coverage"], default=None,
                      help="覆盖契约中的规划目标")
    plan.add_argument("--max-requests", type=int, default=None,
                      help="覆盖契约中的请求预算")
    plan.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    plan.set_defaults(func=_plan_command)

    serve_parser = sub.add_parser("serve", help="启动只读本地复核台")
    _add_db(serve_parser)
    serve_parser.add_argument("--port", type=int, default=5000)
    serve_parser.set_defaults(func=_serve_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ContractError as exc:
        print(f"契约校验失败: {exc}", file=sys.stderr)
        return 2
    except ResumeRejected as exc:
        print(f"拒绝续采: [{exc.code}] {exc.detail}", file=sys.stderr)
        return 3
    except ReplayError as exc:
        print(f"回放文件错误: [{exc.code}] {exc.detail}", file=sys.stderr)
        return 2
    except PipelineError as exc:
        print(f"采集失败: [{exc.code}] {exc.detail}", file=sys.stderr)
        return 1
    except IngestInterrupted as exc:
        print(
            f"采集已中断: 批次 {exc.batch_id} 已处理 {exc.processed_lines} 行；"
            "请使用 --resume 继续。",
            file=sys.stderr,
        )
        return 130
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"文件或数据库错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
