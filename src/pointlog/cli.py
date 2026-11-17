"""Command-line interface for pointlog."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from .contract import ContractError, load_contract
from .drift import (
    DEFAULT_CONSECUTIVE,
    DEFAULT_MAX_GAP_SECONDS,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_THRESHOLD,
    SAMPLINGS,
    DriftError,
    build_baseline,
    detect_drift,
    get_baseline,
    save_baseline,
)
from .fixtures_gen import generate_demo_replay
from .pipeline import IngestInterrupted, PipelineError, RunResult, run_capture
from .replay import ReplayError
from .resume import ResumeRejected
from .server import serve
from .store import Store
from .store_queries import (
    baseline_rows,
    batch_list,
    batch_totals,
    drift_batches,
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


def _drift_baseline_command(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    if not Path(args.db).is_file():
        raise FileNotFoundError(f"database does not exist: {args.db}")
    store = Store(args.db)
    try:
        baseline = build_baseline(
            store,
            training_batch_id=args.training_batch,
            contract=contract,
            sampling=args.sampling,
            min_samples=args.min_samples,
            max_gap_seconds=args.max_gap_seconds,
        )
        baseline_id = save_baseline(store, baseline)
    finally:
        store.close()
    ready = sum(1 for point in baseline["points"] if point["status"] == "ready")
    print(f"基线:         {baseline_id}")
    print(f"训练批次:     {baseline['training_batch']['id']}")
    print(f"采样语义:     {baseline['sampling']}")
    print(f"算法版本:     {baseline['algorithm_version']}")
    print(f"训练摘要:     {baseline['training_digest']}")
    print(f"可用点位:     {ready}/{len(baseline['points'])}")
    if args.format == "json":
        print(json.dumps(baseline, ensure_ascii=False, indent=2))
    return 0


def _drift_detect_command(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    if not Path(args.db).is_file():
        raise FileNotFoundError(f"database does not exist: {args.db}")
    store = Store(args.db)
    try:
        baseline = get_baseline(store, args.baseline)
        report = detect_drift(
            store,
            baseline=baseline,
            detection_batch_id=args.detection_batch,
            contract=contract,
            threshold=args.threshold,
            consecutive=args.consecutive,
        )
        report_paths: dict[str, str] = {}
        if args.reports is not None:
            from .report import write_reports

            report_paths = write_reports(
                store, args.detection_batch, contract, Path(args.reports),
                drift=report,
            )
    finally:
        store.close()
    summary = report["summary"]
    print(f"基线:         {report['baseline']['id']}")
    print(f"采样语义:     {report['sampling']}")
    print(f"检测批次:     {report['detection_batch']['id']}")
    print(f"漂移点位:     {summary['drift_points']}")
    print(f"稳定点位:     {summary['stable_points']}")
    print(f"不足以判断:   {summary['insufficient_points']}")
    print(f"漂移事件:     {summary['drift_events']}")
    for point in report["points"]:
        for event in point["drifts"]:
            print(
                f"  DRIFT {point['device_id']}/{point['point_key']} "
                f"{event['reference']} {event['direction']} "
                f"start={event['start']['ts']} magnitude={event['magnitude']} "
                f"lines={','.join(str(n) for n in event['line_numbers'])}"
            )
        if point["insufficient"]:
            codes = ", ".join(
                item["code"] for item in point["evidence"]
            ) or "insufficient"
            print(
                f"  INSUFFICIENT {point['device_id']}/{point['point_key']} "
                f"reference={point['reference']} evidence={codes}"
            )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    for name, path in report_paths.items():
        print(f"报告[{name}]: {path}")
    return 0


def _drift_baselines_command(args: argparse.Namespace) -> int:
    store = _open_store(args.db)
    try:
        rows = baseline_rows(store, limit=args.limit)
    finally:
        store.close()
    if not rows:
        print("没有冻结基线。")
        return 0
    print(
        f"{'baseline_id':20} {'sampling':8} {'algo':16} "
        f"{'min':>4} {'gap_s':>8}  training_batch"
    )
    for row in rows:
        print(
            f"{row['id']:20} {row['sampling']:8} {row['algorithm_version']:16} "
            f"{row['min_samples']:4d} {row['max_gap_seconds']:8.0f}  "
            f"{row['training_batch_id']}"
        )
    return 0


def _drift_batches_command(args: argparse.Namespace) -> int:
    store = _open_store(args.db)
    try:
        rows = drift_batches(store)
    finally:
        store.close()
    if not rows:
        print("没有已完成的批次。")
        return 0
    print(f"{'batch_id':34} {'lines':>6} {'finished_at':22}  replay_file")
    for batch in rows:
        print(
            f"{batch['id']:34} {int(batch['total_lines']):6d} "
            f"{batch['finished_at'] or '-':22}  {Path(batch['replay_path']).name}"
        )
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

    serve_parser = sub.add_parser("serve", help="启动只读本地复核台")
    _add_db(serve_parser)
    serve_parser.add_argument("--port", type=int, default=5000)
    serve_parser.set_defaults(func=_serve_command)

    drift = sub.add_parser("drift", help="冻结可信参考基线并复核持续漂移")
    drift_sub = drift.add_subparsers(dest="drift_command", required=True)

    baseline_cmd = drift_sub.add_parser(
        "baseline", help="从指定的已完成历史批次冻结参考基线"
    )
    baseline_cmd.add_argument("--batch", required=True, dest="training_batch",
                              help="训练批次 ID（必须已 completed）")
    baseline_cmd.add_argument("--contract", default=str(DEFAULT_CONFIG_PATH))
    _add_db(baseline_cmd)
    baseline_cmd.add_argument("--sampling", choices=SAMPLINGS, default="global",
                              help="采样语义：global=全时段单一，hourly=UTC 小时分层")
    baseline_cmd.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES,
                              help="每个参考层的最低可用样本数")
    baseline_cmd.add_argument("--max-gap-seconds", type=float,
                              default=DEFAULT_MAX_GAP_SECONDS,
                              help="训练/检测允许的最大采样间隔")
    baseline_cmd.add_argument("--format", choices=("text", "json"), default="text")
    baseline_cmd.set_defaults(func=_drift_baseline_command)

    detect_cmd = drift_sub.add_parser(
        "detect", help="用冻结基线复核一个检测批次"
    )
    detect_cmd.add_argument("--baseline", required=True)
    detect_cmd.add_argument("--batch", required=True, dest="detection_batch",
                            help="检测批次 ID（不得与训练批次相同）")
    detect_cmd.add_argument("--contract", default=str(DEFAULT_CONFIG_PATH))
    _add_db(detect_cmd)
    detect_cmd.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                            help="稳健门槛，默认 3 × MAD 尺度")
    detect_cmd.add_argument("--consecutive", type=int, default=DEFAULT_CONSECUTIVE,
                            help="触发漂移所需的连续同向有效采样数")
    detect_cmd.add_argument("--reports", default=None,
                            help="可选：输出含漂移复核章节的报告目录")
    detect_cmd.add_argument("--format", choices=("text", "json"), default="text")
    detect_cmd.set_defaults(func=_drift_detect_command)

    baselines_cmd = drift_sub.add_parser("baselines", help="列出已冻结基线")
    _add_db(baselines_cmd)
    baselines_cmd.add_argument("--limit", type=int, default=50)
    baselines_cmd.set_defaults(func=_drift_baselines_command)

    batches_cmd = drift_sub.add_parser("batches", help="列出可作训练来源的已完成批次")
    _add_db(batches_cmd)
    batches_cmd.set_defaults(func=_drift_batches_command)
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
    except DriftError as exc:
        print(f"漂移复核失败: [{exc.code}] {exc.detail}", file=sys.stderr)
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
