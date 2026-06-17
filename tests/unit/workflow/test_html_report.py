"""
Workflow HTML report 测试.

覆盖:
  1. test_render_workflow_html_basic — 基本 DAG 渲染
  2. test_render_with_result — 含 run result 的状态页面
  3. test_render_mermaid_has_edges — mermaid 有边
  4. test_render_mermaid_failed_node — failed stage 样式
  5. test_render_stage_cards_with_checks — checks 详情渲染
  6. test_visualize_cli_dag_only — CLI visualize 无 run-id
  7. test_visualize_cli_with_run — CLI visualize 含 run-id
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents_chat.workflow import (
    WorkflowRunResult,
    render_and_save_html,
    render_workflow_html,
    load_workflow_from_string,
)


# =============================================================================
# Test pipeline
# =============================================================================


SIMPLE_WF_YAML = """
name: test-dag
stages:
  - id: a
    workers: [{id: wa, cli: mock}]
    deliverable: {path: out/a.json, min_size: 1}
  - id: b
    depends_on: [a]
    workers: [{id: wb, cli: mock}]
    deliverable: {path: out/b.json, min_size: 1}
  - id: c
    depends_on: [a]
    workers: [{id: wc, cli: mock}]
    deliverable: {path: out/c.json, min_size: 1}
  - id: d
    depends_on: [b, c]
    workers: [{id: wd, cli: mock}]
    deliverable: {path: out/d.json, min_size: 1}
"""


# =============================================================================
# render_workflow_html tests
# =============================================================================


class TestRenderWorkflowHtml:
    def test_basic_dag(self):
        """基本 DAG 图渲染."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        html = render_workflow_html(spec)
        assert "<h1>📋 test-dag</h1>" in html
        assert "mermaid" in html
        assert "graph TD" in html
        for sid in ("a", "b", "c", "d"):
            assert sid in html

    def test_mermaid_has_edges(self):
        """Mermaid 有依赖边."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        html = render_workflow_html(spec)
        assert "a --> b" in html
        assert "a --> c" in html
        assert "b --> d" in html
        assert "c --> d" in html

    def test_with_run_result_success(self):
        """含 run result (success)."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        result = WorkflowRunResult("test-dag", "run-ok")
        result.status = "success"
        result.stage_states = {"a": "success", "b": "success", "c": "success", "d": "success"}
        result.finished_at = "2026-06-15T10:05:00"

        html = render_workflow_html(spec, result=result)
        assert "run-ok" in html
        assert "success" in html

    def test_with_run_result_failed(self):
        """含 run result (failed)."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        result = WorkflowRunResult("test-dag", "run-fail")
        result.status = "failed"
        result.failed_stage = "b"
        result.stage_states = {"a": "success", "b": "failed", "c": "pending", "d": "pending"}

        html = render_workflow_html(spec, result=result)
        assert "run-fail" in html
        assert "failed" in html
        assert "Failed at: b" in html

    def test_mermaid_failed_node_style(self):
        """Failed stage 节点有红色 CSS 样式."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        result = WorkflowRunResult("test-dag", "run-x")
        result.stage_states = {"a": "success", "b": "failed", "c": "success", "d": "pending"}

        html = render_workflow_html(spec, result=result)
        # failed 的 b 应有红底样式
        assert 'style b fill:#faa' in html

    def test_stage_cards_rendered(self):
        """Stage cards 含 4 个节点."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        html = render_workflow_html(spec)
        # 4 个 stage card (每个都有 class="stage-card")
        assert html.count('class="stage-card') >= 4

    def test_dark_mode_css(self):
        """响应式 dark mode CSS 存在."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        html = render_workflow_html(spec)
        assert "prefers-color-scheme: dark" in html

    def test_html_completeness(self):
        """HTML 完整: <!DOCTYPE, <html>, </html>."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        html = render_workflow_html(spec)
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_mermaid_no_markdown_fence(self):
        """Mermaid 不用 ```mermaid fence (CDN 解析器只认 <div class='mermaid'>)."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        html = render_workflow_html(spec)
        # 不能有 ``` 包围
        assert "```mermaid" not in html
        # 必须是 <div class="mermaid"> 包装
        assert '<div class="mermaid">' in html
        assert "graph TD" in html


# =============================================================================
# render_and_save_html tests
# =============================================================================


class TestRenderAndSave:
    def test_saves_file(self, tmp_path: Path):
        """保存 HTML 到文件."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        output = tmp_path / "report.html"
        render_and_save_html(spec, str(output))
        assert output.exists()
        content = output.read_text()
        assert "test-dag" in content

    def test_saves_with_result(self, tmp_path: Path):
        """保存含 run result 的 HTML."""
        spec = load_workflow_from_string(SIMPLE_WF_YAML)
        result = WorkflowRunResult("test-dag", "run-1")
        result.status = "success"
        result.stage_states = {"a": "success", "b": "success", "c": "success", "d": "success"}

        output = tmp_path / "report.html"
        render_and_save_html(spec, str(output), result=result)
        assert output.exists()
        content = output.read_text()
        assert "run-1" in content


# =============================================================================
# CLI visualize 集成测试
# =============================================================================


class TestVisualizeCli:
    def test_visualize_dag_only(self, tmp_path: Path):
        """CLI visualize 无 run-id → DAG only."""
        pipeline = tmp_path / "pipeline.yaml"
        pipeline.write_text(SIMPLE_WF_YAML)

        output = tmp_path / "report.html"
        import sys, io
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        exit_code = 0
        try:
            from agents_chat.infra.main import main
            main(["workflow", "visualize", str(pipeline), "-o", str(output)])
        except SystemExit as e:
            exit_code = e.code or 0
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        assert exit_code == 0
        assert output.exists()
        content = output.read_text()
        assert "test-dag" in content
        assert "mermaid" in content

    def test_visualize_missing_pipeline(self, tmp_path: Path):
        """CLI visualize 缺 YAML → exit(1)."""
        import sys, io
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        exit_code = 0
        try:
            from agents_chat.infra.main import main
            main(["workflow", "visualize", "/nonexistent.yaml"])
        except SystemExit as e:
            exit_code = e.code or 0
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        assert exit_code == 1
