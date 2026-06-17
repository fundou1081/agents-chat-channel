"""
workflow — Stage-Isolated Workflow (DAG 编排 + 文件交付).

跟现有 channel 模式**叠加**而非替换:
  - 现有 channel: 持续 listen, 自由 @mention (会议室)
  - workflow: DAG 编排, stage 隔离, 文件交付 (GitHub Actions 风格)

设计文档: docs/26-stage-workflow.md

公共 API:
  - load_workflow(yaml_path) -> WorkflowSpec
  - WorkflowSpec.topological_order() -> list[StageSpec]
  - evaluate_checks(checks, content) -> CheckResult
"""
from .schema import (
    DeliverableSpec,
    WorkerSpec,
    StageSpec,
    WorkflowSpec,
    CheckResult,
    CheckItem,
)
from .loader import load_workflow, load_workflow_from_string
from .checks import evaluate_checks
from .runner import (
    build_system_prompt,
    collect_upstream_inputs,
    spawn_stage_workers,
    build_channel_name,
    build_input_handoff_paths,
)
from .scheduler import WorkflowScheduler, WorkflowRunResult
from .registry import WorkflowRegistry
from .html_report import render_workflow_html, render_and_save_html

__all__ = [
    # Schema
    "DeliverableSpec",
    "WorkerSpec",
    "StageSpec",
    "WorkflowSpec",
    "CheckResult",
    "CheckItem",
    # Loader + checks
    "load_workflow",
    "evaluate_checks",
    # Runner
    "build_system_prompt",
    "collect_upstream_inputs",
    "spawn_stage_workers",
    "build_channel_name",
    "build_input_handoff_paths",
    # Scheduler
    "WorkflowScheduler",
    "WorkflowRunResult",
    "WorkflowRegistry",
    # HTML Report
    "render_workflow_html",
    "render_and_save_html",
]
