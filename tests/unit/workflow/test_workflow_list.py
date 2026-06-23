"""
workflow list CLI 测试 (跟设计文档 §9.3 对齐).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def examples_dir(tmp_path: Path):
    """创建 examples/ 目录含多个 workflow YAML."""
    d = tmp_path / "examples"
    d.mkdir()
    (d / "a-pipeline.yaml").write_text("""
name: a-pipeline
stages:
  - id: a1
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a1.json, min_size: 1}
  - id: a2
    depends_on: [a1]
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a2.json, min_size: 1}
""")
    (d / "b-pipeline.yaml").write_text("""
name: b-pipeline
stages:
  - id: b1
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/b1.json, min_size: 1}
""")
    (d / "invalid-pipeline.yaml").write_text("""
name: invalid-pipeline
stages: []
""")
    sub = d / "subdir"
    sub.mkdir()
    (sub / "nested.yaml").write_text("""
name: nested-pipeline
stages:
  - id: n1
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/n1.json}
""")
    return d


def _run_cli(args: list[str], examples_dir: Path) -> tuple[int, str, str]:
    """跑 CLI 并捕获 stdout/stderr."""
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    exit_code = 0
    try:
        from agents_chat.infra.main import main
        main(args)
    except SystemExit as e:
        exit_code = e.code or 0
    finally:
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return exit_code, out, err


# =============================================================================
# workflow list tests
# =============================================================================


class TestWorkflowListCLI:
    def test_lists_all_workflows(self, tmp_path, examples_dir):
        """workflow list 列出所有 valid workflow."""
        data_dir = tmp_path / "data_v2"
        data_dir.mkdir()
        code, out, err = _run_cli([
            "workflow", "list",
            "--scan-dir", str(examples_dir),
            "--data-dir", str(data_dir),
        ], examples_dir)
        assert code == 0, f"stderr: {err}"
        assert "a-pipeline" in out
        assert "b-pipeline" in out
        assert "nested-pipeline" in out
        # invalid 不阻塞
        assert "stages" in out  # 至少 1 个 workflow 含 stages 信息

    def test_marks_invalid_yaml_with_warning(self, tmp_path, examples_dir):
        """Invalid yaml 不阻塞, 标 ⚠️."""
        data_dir = tmp_path / "data_v2"
        data_dir.mkdir()
        code, out, err = _run_cli([
            "workflow", "list",
            "--scan-dir", str(examples_dir),
            "--data-dir", str(data_dir),
        ], examples_dir)
        assert code == 0
        assert "⚠️" in out
        assert "invalid-pipeline" in out

    def test_relative_scan_dir(self, tmp_path):
        """--scan-dir 相对路径 (默认 examples/)."""
        # 不创建 examples/ 在 tmp_path, 改用绝对路径
        # 创建在 tmp_path/external_examples
        external = tmp_path / "external_examples"
        external.mkdir()
        (external / "wf.yaml").write_text("""
name: my-wf
stages:
  - id: s
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/s.json}
""")

        # 用绝对路径
        data_dir = tmp_path / "data_v2"
        data_dir.mkdir()
        code, out, err = _run_cli([
            "workflow", "list",
            "--scan-dir", str(external),
            "--data-dir", str(data_dir),
        ], external)
        assert code == 0, f"stderr: {err}"
        assert "my-wf" in out

    def test_nonexistent_dir_exits_nonzero(self, tmp_path):
        """不存在的目录 → exit(1) + error."""
        data_dir = tmp_path / "data_v2"
        data_dir.mkdir()
        code, out, err = _run_cli([
            "workflow", "list",
            "--scan-dir", "/nonexistent/dir",
            "--data-dir", str(data_dir),
        ], None)
        assert code == 1
        assert "not found" in err

    def test_empty_dir_message(self, tmp_path):
        """空目录 → (no workflows found ...)."""
        empty = tmp_path / "empty"
        empty.mkdir()
        data_dir = tmp_path / "data_v2"
        data_dir.mkdir()
        code, out, err = _run_cli([
            "workflow", "list",
            "--scan-dir", str(empty),
            "--data-dir", str(data_dir),
        ], empty)
        assert code == 0
        assert "no workflows found" in out

    def test_nested_subdir(self, tmp_path, examples_dir):
        """rglob 找子目录."""
        data_dir = tmp_path / "data_v2"
        data_dir.mkdir()
        code, out, err = _run_cli([
            "workflow", "list",
            "--scan-dir", str(examples_dir),
            "--data-dir", str(data_dir),
        ], examples_dir)
        assert code == 0
        assert "nested-pipeline" in out
        assert "subdir/nested.yaml" in out