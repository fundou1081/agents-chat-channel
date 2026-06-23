"""
Round 5 修复的测试 - API 跟设计文档 §9.2 对齐.

新增的端点 (跟设计文档 §9.2 完全一致):
  GET  /api/workflows/{name}                       读 spec
  POST /api/workflows/{name}/runs                 启新 run by name
  GET  /api/workflows/{name}/runs/{run_id}        查 run (URL 含 name)
  POST /api/workflows/{name}/runs/{run_id}/cancel 取消 (URL 含 name)
"""
from __future__ import annotations

import json
import sys
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def setup_data_dir(tmp_path: Path):
    """初始化 data_v2 + 在 tmp_path/examples/ 放 sample workflows."""
    # data_dir
    d = tmp_path / "data_v2"
    for sub in ["channels", "mailboxes", "sessions", "locks", "logs"]:
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "state_board.json").write_text('{"tasks":{},"updated_at":""}')
    (d / "channels" / "general.jsonl").touch()

    # examples/ 目录 (在 data_dir 之上)
    examples = tmp_path / "examples"
    examples.mkdir(parents=True, exist_ok=True)
    (examples / "test-pipeline.yaml").write_text("""
name: test-pipeline
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json, min_size: 1}
    timeout: 5
""")
    (examples / "invalid-pipeline.yaml").write_text("""
name: invalid-pipeline
stages: []
""")
    return d


@pytest.fixture
def client(setup_data_dir):
    from agents_chat.infra.server import create_app
    return TestClient(create_app(data_dir=setup_data_dir))


@pytest.fixture
def save_run(setup_data_dir):
    """在 runs/ 写一个 run state 文件."""
    def _save(run_id: str, workflow_name: str = "test-pipeline",
              status: str = "success", failed_stage: str = None):
        runs_dir = setup_data_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        data = {
            "workflow_name": workflow_name,
            "run_id": run_id,
            "status": status,
            "started_at": "2026-06-15T10:00:00",
            "finished_at": "2026-06-15T10:05:00",
            "failed_stage": failed_stage,
            "stage_states": {"a": "success"},
            "check_results": {},
            "stage_deps": {"a": []},
        }
        run_file = runs_dir / f"{run_id}.json"
        run_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return run_file
    return _save


# =============================================================================
# GET /api/workflows/{name} — 读 spec
# =============================================================================


class TestGetWorkflowSpec:
    def test_existing_workflow(self, client):
        """读已注册的 workflow spec."""
        resp = client.get("/api/workflows/test-pipeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-pipeline"
        assert data["yaml_path"] == "examples/test-pipeline.yaml"
        assert len(data["stages"]) == 1
        assert data["stages"][0]["id"] == "a"
        assert "w" in data["stages"][0]["workers"]

    def test_nonexistent_workflow(self, client):
        """name 不存在 → 404."""
        resp = client.get("/api/workflows/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_invalid_workflow_yaml(self, client):
        """YAML 解析失败 → 400."""
        resp = client.get("/api/workflows/invalid-pipeline")
        assert resp.status_code == 400
        assert "invalid" in resp.json()["detail"].lower()

    def test_spec_contains_deliverable(self, client):
        """spec 含 deliverable 详情."""
        resp = client.get("/api/workflows/test-pipeline")
        data = resp.json()
        stage = data["stages"][0]
        d = stage["deliverable"]
        assert d["path"] == "out/a.json"
        assert d["min_size"] == 1
        assert d["max_size"] is None
        assert d["checks"] == []


# =============================================================================
# POST /api/workflows/{name}/runs — 启新 run by name
# =============================================================================


class TestRunByName:
    def test_start_success(self, client, setup_data_dir):
        """POST by name 启新 run."""
        # 预写 deliverable
        (setup_data_dir / "out").mkdir()
        (setup_data_dir / "out" / "a.json").write_text("## ok")

        resp = client.post(
            "/api/workflows/test-pipeline/runs",
            json={},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "run_id" in data
        assert data["workflow"] == "test-pipeline"
        assert data["stages"] == 1

    def test_start_with_from_stage(self, client, setup_data_dir):
        """from_stage flag."""
        (setup_data_dir / "out").mkdir()
        (setup_data_dir / "out" / "a.json").write_text("## ok")

        resp = client.post(
            "/api/workflows/test-pipeline/runs",
            json={"from_stage": "a"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["from_stage"] == "a"

    def test_start_with_single_stage(self, client, setup_data_dir):
        """single_stage flag."""
        (setup_data_dir / "out").mkdir()
        (setup_data_dir / "out" / "a.json").write_text("## ok")

        resp = client.post(
            "/api/workflows/test-pipeline/runs",
            json={"single_stage": "a"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["single_stage"] == "a"

    def test_nonexistent_workflow_404(self, client):
        """name 不存在 → 404."""
        resp = client.post(
            "/api/workflows/nonexistent/runs",
            json={},
        )
        assert resp.status_code == 404


# =============================================================================
# GET /api/workflows/{name}/runs/{run_id} — 查 run (URL 含 name)
# =============================================================================


class TestGetRunByName:
    def test_existing(self, client, save_run):
        """name + run_id 匹配 → 返 run."""
        save_run("run-abc", "test-pipeline")
        resp = client.get("/api/workflows/test-pipeline/runs/run-abc")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run-abc"
        assert data["workflow_name"] == "test-pipeline"

    def test_wrong_name_404(self, client, save_run):
        """name 不匹配 run 的 workflow_name → 404 (不是 leak 别人的 run)."""
        save_run("run-abc", "test-pipeline")
        resp = client.get("/api/workflows/different-pipeline/runs/run-abc")
        assert resp.status_code == 404
        assert "does not belong" in resp.json()["detail"]

    def test_nonexistent_run_404(self, client):
        """run_id 不存在 → 404."""
        resp = client.get("/api/workflows/test-pipeline/runs/nonexistent")
        assert resp.status_code == 404

    def test_invalid_run_file(self, client, setup_data_dir):
        """run JSON 损坏 → 500."""
        runs_dir = setup_data_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        (runs_dir / "run-bad.json").write_text("{ not valid json")
        resp = client.get("/api/workflows/test-pipeline/runs/run-bad")
        assert resp.status_code == 500


# =============================================================================
# POST /api/workflows/{name}/runs/{run_id}/cancel — 取消 (URL 含 name)
# =============================================================================


class TestCancelByName:
    def test_cancel_success(self, client, save_run, setup_data_dir):
        """name + run_id 匹配 + active → cancel."""
        from agents_chat.workflow.registry import WorkflowRegistry
        from unittest.mock import MagicMock
        mock_sched = MagicMock()
        mock_sched.run_id = "run-cancel"
        mock_sched.workflow.name = "test-pipeline"
        WorkflowRegistry.get_default().register(mock_sched)

        resp = client.post("/api/workflows/test-pipeline/runs/run-cancel/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "canceled"
        assert data["workflow"] == "test-pipeline"
        assert mock_sched.cancel.called  # cancel 方法被调

    def test_wrong_name_404(self, client, save_run):
        """name 不匹配 → 404."""
        from agents_chat.workflow.registry import WorkflowRegistry
        from unittest.mock import MagicMock
        mock_sched = MagicMock()
        mock_sched.run_id = "run-x"
        mock_sched.workflow.name = "test-pipeline"
        WorkflowRegistry.get_default().register(mock_sched)

        resp = client.post("/api/workflows/different-pipeline/runs/run-x/cancel")
        assert resp.status_code == 404

    def test_not_in_registry_404(self, client):
        """run 不在 active registry → 404."""
        resp = client.post("/api/workflows/test-pipeline/runs/nonexistent/cancel")
        assert resp.status_code == 404
        assert "not in active registry" in resp.json()["detail"]


# =============================================================================
# 回归测试: 老的端点还能用 (向后兼容)
# =============================================================================


class TestBackwardCompat:
    """老的 endpoint (path 不带 name) 应该继续工作."""

    def test_old_list_runs(self, client, save_run):
        """GET /api/workflows (老, 列 runs) 还工作."""
        save_run("run-x", "test-pipeline")
        resp = client.get("/api/workflows?limit=10")
        assert resp.status_code == 200
        assert "runs" in resp.json()

    def test_old_get_run(self, client, save_run):
        """GET /api/workflows/{run_id} (老, 不带 name) 还工作."""
        save_run("run-y", "test-pipeline")
        resp = client.get("/api/workflows/run-y")
        assert resp.status_code == 200
        assert resp.json()["run_id"] == "run-y"

    def test_old_cancel(self, client):
        """POST /api/workflows/{run_id}/cancel (老, 不带 name) 还工作."""
        from agents_chat.workflow.registry import WorkflowRegistry
        from unittest.mock import MagicMock
        mock_sched = MagicMock()
        mock_sched.run_id = "run-z"
        mock_sched.workflow.name = "test-pipeline"
        WorkflowRegistry.get_default().register(mock_sched)

        resp = client.post("/api/workflows/run-z/cancel")
        assert resp.status_code == 200
