"""
Workflow REST API 测试.

覆盖:
  1. test_list_runs_empty — GET /api/workflows 空列表
  2. test_list_runs_with_data — GET /api/workflows 含历史
  3. test_get_run — GET /api/workflows/{run_id} 详情
  4. test_get_run_404 — GET /api/workflows/{run_id} 不存在 → 404
  5. test_validate — POST /api/workflows/validate 成功
  6. test_validate_invalid — POST /api/workflows/validate 非法 YAML
  7. test_run_workflow — POST /api/workflows/run 成功
  8. test_run_workflow_missing_yaml — POST /api/workflows/run 文件不存在 → 404
"""
from __future__ import annotations

import json
import sys
import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """初始化 data_v2."""
    d = tmp_path / "data_v2"
    for sub in ["channels", "mailboxes", "sessions", "locks", "logs"]:
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "state_board.json").write_text('{"tasks":{},"updated_at":""}')
    (d / "channels" / "general.jsonl").touch()
    return d


@pytest.fixture
def client(data_dir: Path):
    """创建 FastAPI TestClient."""
    from agents_chat.infra.server import create_app
    app = create_app(data_dir=data_dir)
    return TestClient(app)


@pytest.fixture
def sample_yaml(data_dir: Path) -> Path:
    """创建 sample pipeline YAML."""
    yaml_path = data_dir / "sample-pipeline.yaml"
    yaml_path.write_text("""
name: api-test
stages:
  - id: a
    workers: [{id: wa, cli: mock}]
    deliverable: {path: out/a.json, min_size: 1}
    timeout: 5
""")
    return yaml_path


@pytest.fixture
def sample_run(data_dir: Path) -> Path:
    """创建 sample run 持久化文件."""
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(exist_ok=True)
    runs_dir.mkdir(exist_ok=True)
    run_file = runs_dir / "run-abc12345.json"
    run_file.write_text(json.dumps({
        "workflow_name": "api-test",
        "run_id": "run-abc12345",
        "status": "success",
        "started_at": "2026-06-15T10:00:00",
        "finished_at": "2026-06-15T10:05:00",
        "failed_stage": None,
        "stage_states": {"a": "success", "b": "success"},
        "check_results": {
            "a": {"all_passed": True, "items": [
                {"type": "contains", "passed": True, "detail": "matched 1/1", "value": "ok"}
            ]},
        },
    }))
    return run_file


# =============================================================================
# GET /api/workflows
# =============================================================================


class TestListRuns:
    def test_empty(self, client):
        """无 runs → 空列表."""
        resp = client.get("/api/workflows")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runs"] == []

    def test_with_runs(self, client, sample_run):
        """含 runs → 列表."""
        resp = client.get("/api/workflows")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["runs"]) == 1
        assert data["runs"][0]["run_id"] == "run-abc12345"
        assert data["runs"][0]["status"] == "success"

    def test_limit(self, client, data_dir):
        """--limit 限制."""
        runs_dir = data_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        for i in range(5):
            (runs_dir / f"run-{i:03d}.json").write_text(json.dumps({
                "workflow_name": "test", "run_id": f"run-{i:03d}",
                "status": "success", "started_at": "", "stage_states": {},
            }))

        resp = client.get("/api/workflows?limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["runs"]) == 2


# =============================================================================
# GET /api/workflows/{run_id}
# =============================================================================


class TestGetRun:
    def test_existing(self, client, sample_run):
        """获取已有 run 详情."""
        resp = client.get("/api/workflows/run-abc12345")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == "run-abc12345"
        assert data["status"] == "success"
        assert data["stage_states"] == {"a": "success", "b": "success"}
        assert "check_results" in data

    def test_not_found(self, client):
        """不存在的 run → 404."""
        resp = client.get("/api/workflows/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# =============================================================================
# POST /api/workflows/validate
# =============================================================================


class TestValidate:
    def test_valid(self, client, sample_yaml):
        """验证合法 YAML."""
        resp = client.post("/api/workflows/validate", json={
            "yaml_path": str(sample_yaml),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["name"] == "api-test"
        assert len(data["stages"]) == 1
        assert data["stages"][0]["id"] == "a"

    def test_invalid(self, client, data_dir):
        """验证非法 YAML → valid: false."""
        bad_yaml = data_dir / "bad.yaml"
        bad_yaml.write_text("name: bad\nstages: []\n")
        resp = client.post("/api/workflows/validate", json={
            "yaml_path": str(bad_yaml),
        })
        assert resp.status_code == 200  # 200 返 valid:false
        data = resp.json()
        assert data["valid"] is False
        assert "error" in data

    def test_missing_file(self, client):
        """文件不存在 → 404."""
        resp = client.post("/api/workflows/validate", json={
            "yaml_path": "/nonexistent/pipeline.yaml",
        })
        assert resp.status_code == 404


# =============================================================================
# POST /api/workflows/run
# =============================================================================


class TestRunWorkflow:
    def test_start_success(self, client, sample_yaml, data_dir):
        """启动 workflow run → 返 run_id."""
        # 预写 deliverable 防止 hang
        (data_dir / "out").mkdir(exist_ok=True)
        (data_dir / "out" / "a.json").write_text('{"ok": true}')

        resp = client.post("/api/workflows/run", json={
            "yaml_path": str(sample_yaml),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "run_id" in data
        assert data["workflow"] == "api-test"
        assert data["stages"] == 1

    def test_missing_yaml(self, client):
        """文件不存在 → 404."""
        resp = client.post("/api/workflows/run", json={
            "yaml_path": "/nonexistent/pipeline.yaml",
        })
        assert resp.status_code == 404

    def test_invalid_yaml(self, client, data_dir):
        """非法 YAML → 400."""
        bad_yaml = data_dir / "bad.yaml"
        bad_yaml.write_text("name: bad\nstages: []\n")
        resp = client.post("/api/workflows/run", json={
            "yaml_path": str(bad_yaml),
        })
        assert resp.status_code == 400

    def test_with_flags(self, client, sample_yaml, data_dir):
        """--from-stage / --single-stage flag."""
        (data_dir / "out").mkdir(exist_ok=True)
        (data_dir / "out" / "a.json").write_text('{"ok": true}')

        resp = client.post("/api/workflows/run", json={
            "yaml_path": str(sample_yaml),
            "from_stage": "a",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["from_stage"] == "a"
        assert data["single_stage"] is None


# =============================================================================
# GET /api/workflows/{run_id}/html
# =============================================================================


class TestWorkflowHTML:
    def test_html_rendered(self, client, sample_run):
        """HTML 可视化页面."""
        resp = client.get("/api/workflows/run-abc12345/html")
        assert resp.status_code == 200
        html = resp.text
        assert "<!DOCTYPE html>" in html
        assert "api-test" in html
        assert "mermaid" in html

    def test_html_404(self, client):
        """不存在的 run → 404."""
        resp = client.get("/api/workflows/nonexistent/html")
        assert resp.status_code == 404

# =============================================================================
# R7/R8: cancel / active endpoints
# =============================================================================


class TestCancelAndActive:
    def test_active_empty(self, client):
        """GET /api/workflows/active → 空 list."""
        resp = client.get("/api/workflows/active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] == []

    def test_cancel_not_found(self, client):
        """POST /api/workflows/{run_id}/cancel — 不存在 → 404."""
        resp = client.post("/api/workflows/nonexistent/cancel")
        assert resp.status_code == 404
        assert "not found in active registry" in resp.json()["detail"]

    def test_cancel_success(self, client, data_dir):
        """POST /api/workflows/{run_id}/cancel — active → cancel."""
        from agents_chat.workflow.registry import WorkflowRegistry
        from agents_chat.workflow import load_workflow_from_string, WorkflowScheduler

        # 创建 workflow YAML + 预写 deliverable
        yaml_path = data_dir / "pipeline.yaml"
        yaml_path.write_text("""
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json, min_size: 1}
    timeout: 30
""")
        (data_dir / "out").mkdir()
        (data_dir / "out" / "a.json").write_text("## ok")

        # 注册 scheduler (不实际跑, 只调 cancel)
        spec = load_workflow_from_string(yaml_path.read_text())
        scheduler = WorkflowScheduler(spec, data_dir, run_id="run-cancel-1")
        registry = WorkflowRegistry.get_default()
        registry.register(scheduler)

        # 验证 active
        active = client.get("/api/workflows/active").json()
        assert "run-cancel-1" in active["active"]

        # 取消
        resp = client.post("/api/workflows/run-cancel-1/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "canceled"
        assert scheduler._cancel_requested is True

        # 清理
        registry.unregister("run-cancel-1")
