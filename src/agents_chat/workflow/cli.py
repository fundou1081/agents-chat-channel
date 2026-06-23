"""
Workflow CLI 子命令.

Usage:
  python -m agents_chat workflow run pipeline.yaml [--from-stage S] [--single-stage S] [--data-dir DIR]
  python -m agents_chat workflow list-runs [--data-dir DIR]
  python -m agents_chat workflow status RUN_ID [--data-dir DIR]
  python -m agents_chat workflow validate pipeline.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import pydantic
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("workflow-cli")


# =============================================================================
# Handlers
# =============================================================================


def cmd_run(args: argparse.Namespace) -> None:
    """运行 workflow pipeline."""
    from ..workflow import load_workflow, WorkflowScheduler

    yaml_path = Path(args.yaml_path).resolve()
    data_dir = Path(args.data_dir).resolve()

    # 1. 加载
    try:
        spec = load_workflow(yaml_path)
    except (FileNotFoundError, ValueError, ImportError) as e:
        print(f"❌ 加载 workflow 失败: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"📋 Workflow: {spec.name}")
    print(f"   Description: {spec.description or '(none)'}")
    print(f"   Stages: {len(spec.stages)}")
    for s in spec.topological_order():
        dep_str = f" (depends: {s.depends_on})" if s.depends_on else ""
        print(f"     • {s.id}: {len(s.workers)} worker(s), timeout={s.timeout}s{dep_str}")
    print()

    # 2. 跑
    print(f"🚀 启动 run...")
    scheduler = WorkflowScheduler(
        spec,
        data_dir=data_dir,
        from_stage=args.from_stage,
        single_stage=args.single_stage,
    )

    result = asyncio.run(scheduler.run())

    # 3. 结果
    print()
    print("=" * 50)
    print(f"🏁 Workflow: {result.workflow_name}")
    print(f"   Run ID: {result.run_id}")
    print(f"   Status: {result.status.upper()}")
    if result.failed_stage:
        print(f"   Failed at: {result.failed_stage}")
    print()
    print("   Stage states:")
    for sid, state in result.stage_states.items():
        icon = {"success": "✅", "failed": "❌", "running": "🔄"}.get(state, "•")
        print(f"     {icon} {sid}: {state}")

    if result.status != "success":
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    """列已注册的 workflow (扫盘 YAML, 跟设计文档 §9.3 对齐).

    类似 list-runs, 但列 workflow, 不是 runs.
    """
    from ..workflow.loader import load_workflow

    scan_path = Path(args.scan_dir)
    if not scan_path.is_absolute():
        # 默认相对 data_dir 的父目录
        data_dir = Path(args.data_dir).resolve()
        scan_path = data_dir.parent / scan_path
    if not scan_path.is_dir():
        print(f"❌ directory not found: {scan_path}", file=sys.stderr)
        sys.exit(1)

    found = []
    for yaml_file in sorted(scan_path.rglob("*.yaml")) + sorted(scan_path.rglob("*.yml")):
        if any(part in yaml_file.parts for part in ("node_modules", ".git", "__pycache__")):
            continue
        try:
            spec = load_workflow(yaml_file)
            stages = spec.topological_order()
            found.append({
                "yaml": str(yaml_file.relative_to(scan_path)),
                "name": spec.name,
                "stages": len(stages),
                "stage_ids": [s.id for s in stages],
            })
        except (ValueError, FileNotFoundError) as e:
            found.append({
                "yaml": str(yaml_file.relative_to(scan_path)),
                "error": str(e),
            })

    if not found:
        print(f"(no workflows found in {scan_path})")
        return

    print(f"Workflows ({len(found)}):")
    for f in found:
        if "error" in f:
            print(f"  ⚠️  {f['yaml']}: {f['error']}")
        else:
            stages_str = ", ".join(f["stage_ids"])
            print(f"  📋 {f['name']}  ({f['stages']} stages: {stages_str})  [{f['yaml']}]")


def cmd_list_runs(args: argparse.Namespace) -> None:
    """列最近 workflow runs."""
    data_dir = Path(args.data_dir).resolve()
    runs_dir = data_dir / "runs"
    if not runs_dir.is_dir():
        print("(no runs yet)")
        return

    run_files = sorted(runs_dir.glob("run-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not run_files:
        print("(no runs yet)")
        return

    count = min(len(run_files), args.limit)
    print(f"Recent runs ({count}/{len(run_files)}):\n")
    for rf in run_files[:count]:
        try:
            data = json.loads(rf.read_text("utf-8"))
            wf_name = data.get("workflow_name", "?")
            run_id = data.get("run_id", rf.stem)
            status = data.get("status", "?")
            started = data.get("started_at", "?")[:19]
            failed = data.get("failed_stage")
            icon = {"success": "✅", "failed": "❌", "running": "🔄"}.get(status, "•")
            failed_str = f" at {failed}" if failed else ""
            print(f"  {icon} {run_id}  {wf_name}  [{status}{failed_str}]  {started}")
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  ⚠️ {rf.name}: parse error ({e})")


def cmd_status(args: argparse.Namespace) -> None:
    """看指定 run 的详细状态."""
    data_dir = Path(args.data_dir).resolve()
    run_file = data_dir / "runs" / f"{args.run_id}.json"

    if not run_file.exists():
        print(f"❌ run '{args.run_id}' not found", file=sys.stderr)
        sys.exit(1)

    data = json.loads(run_file.read_text("utf-8"))
    print(f"Run: {data.get('run_id')}")
    print(f"Workflow: {data.get('workflow_name')}")
    print(f"Status: {data.get('status')}")
    print(f"Started: {data.get('started_at')}")
    print(f"Finished: {data.get('finished_at')}")
    if data.get("failed_stage"):
        print(f"Failed at: {data['failed_stage']}")
    print()
    print("Stage states:")
    for sid, state in data.get("stage_states", {}).items():
        check_info = ""
        if "check_results" in data and sid in data["check_results"]:
            cr = data["check_results"][sid]
            if "all_passed" in cr:
                check_info = f" (checks: {'✓' if cr['all_passed'] else '✗'})"
        icon = {"success": "✅", "failed": "❌", "running": "🔄"}.get(state, "•")
        print(f"  {icon} {sid}: {state}{check_info}")


def cmd_validate(args: argparse.Namespace) -> None:
    """验证 workflow YAML 语法."""
    from ..workflow import WorkflowSpec

    yaml_path = Path(args.yaml_path).resolve()
    try:
        raw = yaml.safe_load(yaml_path.read_text("utf-8"))
        spec = WorkflowSpec.model_validate(raw)
        stages = spec.topological_order()
        print(f"✅ Valid workflow: {spec.name}")
        print(f"   Stages: {len(spec.stages)} (topological order: {[s.id for s in stages]})")
        for s in stages:
            print(f"   • {s.id}: {len(s.workers)} worker(s), timeout={s.timeout}s")
    except (FileNotFoundError, ValueError, yaml.YAMLError, pydantic.ValidationError) as e:
        print(f"❌ Validation failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_cancel(args: argparse.Namespace) -> None:
    """取消 running workflow (需要 server 启)."""
    import urllib.request
    base = args.server_url.rstrip("/")
    url = f"{base}/api/workflows/{args.run_id}/cancel"
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        print(f"✅ {data.get('message', 'cancelled')}")
        print(f"   Run: {data.get('run_id')}")
        print(f"   Status: {data.get('status')}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"❌ Run '{args.run_id}' not found in server registry", file=sys.stderr)
            print("   (可能: 1) run 已完成 2) server URL 错 3) 启方式是 CLI)", file=sys.stderr)
            sys.exit(1)
        print(f"❌ HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ Failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_active(args: argparse.Namespace) -> None:
    """列 server 上 active workflows."""
    import urllib.request
    base = args.server_url.rstrip("/")
    url = f"{base}/api/workflows/active"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        active = data.get("active", [])
        if not active:
            print("(no active workflows)")
        else:
            print(f"Active workflows ({len(active)}):")
            for rid in active:
                print(f"  🔄 {rid}")
    except Exception as e:
        print(f"❌ Failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_visualize(args: argparse.Namespace) -> None:
    """生成 DAG + stage 状态 HTML 页面."""
    from ..workflow import load_workflow, WorkflowRunResult
    from ..workflow.html_report import render_and_save_html

    yaml_path = Path(args.yaml_path).resolve()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else Path("./data_v2")

    # 加载 workflow
    try:
        spec = load_workflow(yaml_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"❌ 加载 workflow 失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 尝试加载 run result (如果指定了 run_id)
    result = None
    if args.run_id:
        run_file = data_dir / "runs" / f"{args.run_id}.json"
        if run_file.exists():
            try:
                data = json.loads(run_file.read_text("utf-8"))
                result = WorkflowRunResult(
                    workflow_name=data.get("workflow_name", spec.name),
                    run_id=data.get("run_id", args.run_id),
                    status=data.get("status", "unknown"),
                    started_at=data.get("started_at"),
                    finished_at=data.get("finished_at"),
                    failed_stage=data.get("failed_stage"),
                    stage_states=data.get("stage_states", {}),
                )
            except (json.JSONDecodeError, KeyError):
                pass

    # 输出
    output_path = args.output or f"workflow-{spec.name}.html"
    render_and_save_html(spec, output_path, result)
    print(f"📋 {spec.name}: {len(spec.stages)} stages")
    if result:
        print(f"   Run: {result.run_id} [{result.status}]")


# =============================================================================
# Parser registration (called from infra/main.py)
# =============================================================================


def register_workflow_parser(subparsers: argparse._SubParsersAction) -> None:
    """注册 workflow 子命令组."""
    p_wf = subparsers.add_parser(
        "workflow",
        help="Stage-isolated workflow 编排",
        description="Run multi-stage workflows with file-based stage isolation.",
    )
    wf_sub = p_wf.add_subparsers(metavar="subcommand")

    # ---- workflow run ----
    p_run = wf_sub.add_parser("run", help="运行 pipeline YAML")
    p_run.add_argument("yaml_path", help="Pipeline YAML 文件路径")
    p_run.add_argument("--from-stage", default=None, help="从指定 stage 开始 (跳过前置)")
    p_run.add_argument("--single-stage", default=None, help="只跑一个 stage (用于 retry)")
    p_run.add_argument("--data-dir", default=os.environ.get("AGENTS_CHAT_DATA_DIR", "./data_v2"))
    p_run.set_defaults(cmd="workflow-run")

    # ---- workflow list-runs ----
    p_list = wf_sub.add_parser("list-runs", help="列所有 workflow runs")
    p_list.add_argument("--limit", type=int, default=20, help="最多显示 N 条 (default 20)")
    p_list.add_argument("--data-dir", default=os.environ.get("AGENTS_CHAT_DATA_DIR", "./data_v2"))
    p_list.set_defaults(cmd="workflow-list-runs")

    # ---- workflow status ----
    p_status = wf_sub.add_parser("status", help="看 run 详细状态")
    p_status.add_argument("run_id", help="Run ID (e.g. run-abc12345)")
    p_status.add_argument("--data-dir", default=os.environ.get("AGENTS_CHAT_DATA_DIR", "./data_v2"))
    p_status.set_defaults(cmd="workflow-status")

    # ---- workflow validate ----
    p_validate = wf_sub.add_parser("validate", help="验证 pipeline YAML 语法")
    p_validate.add_argument("yaml_path", help="Pipeline YAML 文件路径")
    p_validate.set_defaults(cmd="workflow-validate")

    # ---- workflow visualize ----
    p_viz = wf_sub.add_parser("visualize", help="生成 DAG + stage 状态 HTML 页面")
    p_viz.add_argument("yaml_path", help="Pipeline YAML 文件路径")
    p_viz.add_argument("--run-id", default=None, help="Run ID (可选, 有则显示 status)")
    p_viz.add_argument("--output", "-o", default=None, help="输出文件路径 (默认 workflow-<name>.html)")
    p_viz.add_argument("--data-dir", default=os.environ.get("AGENTS_CHAT_DATA_DIR", "./data_v2"))
    p_viz.set_defaults(cmd="workflow-visualize")

    # ---- workflow cancel ----
    p_cancel = wf_sub.add_parser("cancel", help="取消 server 上 running workflow")
    p_cancel.add_argument("run_id", help="Run ID")
    p_cancel.add_argument("--server-url", default=os.environ.get("AGENTS_CHAT_SERVER_URL", "http://127.0.0.1:8765"), help="Server URL")
    p_cancel.set_defaults(cmd="workflow-cancel")

    # ---- workflow active ----
    p_active = wf_sub.add_parser("active", help="列 server 上 active workflows")
    p_active.add_argument("--server-url", default=os.environ.get("AGENTS_CHAT_SERVER_URL", "http://127.0.0.1:8765"), help="Server URL")
    p_active.set_defaults(cmd="workflow-active")

    # ---- workflow list ----
    p_list_wf = wf_sub.add_parser("list", help="列已注册的 workflow (扫盘 YAML, 跟设计文档 §9.3 对齐)")
    p_list_wf.add_argument("--scan-dir", default="examples", help="扫的目录 (相对 data_dir 的父目录)")
    p_list_wf.add_argument("--data-dir", default=os.environ.get("AGENTS_CHAT_DATA_DIR", "./data_v2"))
    p_list_wf.set_defaults(cmd="workflow-list")
