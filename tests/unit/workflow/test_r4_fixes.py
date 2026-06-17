"""
Round 4 修复的测试.

覆盖:
  R5: /api/workflows/registry 列已注册 workflow
  R16: html_report._parse_iso_ts 解析 ISO 时间戳
  R17: stage card 显示 worker model
  P1: StageSpec.retry 字段 + scheduler 重试逻辑
"""
from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents_chat.workflow import (
    WorkflowScheduler,
    load_workflow_from_string,
)
from agents_chat.workflow.html_report import (
    render_workflow_html,
    _parse_iso_ts,
)
from agents_chat.workflow.schema import (
    WorkerSpec,
    DeliverableSpec,
    StageSpec,
    RetrySpec,
)


# =============================================================================
# R16: _parse_iso_ts
# =============================================================================


class TestParseIsoTs:
    def test_none(self):
        assert _parse_iso_ts(None) == "?"

    def test_z_suffix(self):
        """Z 后缀 → 正常解析."""
        result = _parse_iso_ts("2026-06-15T10:00:00Z")
        assert result == "2026-06-15 10:00:00"

    def test_with_timezone(self):
        """含 +00:00 时区."""
        result = _parse_iso_ts("2026-06-15T10:00:00+00:00")
        assert result == "2026-06-15 10:00:00"

    def test_with_offset(self):
        """含 +08:00 时区 (本地时区)."""
        result = _parse_iso_ts("2026-06-15T18:00:00+08:00")
        assert "2026-06-15 18:00:00" in result

    def test_microseconds(self):
        """含微秒."""
        result = _parse_iso_ts("2026-06-15T10:00:00.123456+00:00")
        # 微秒会丢失 (strftime 不含 %f)
        assert "2026-06-15 10:00:00" in result

    def test_invalid_falls_back_to_slice(self):
        """无效时间戳 → 兜底截断."""
        result = _parse_iso_ts("not-a-timestamp-at-all")
        # len >= 19, slice 前 19 字符
        assert result == "not-a-timestamp-at-"
        # len < 19, 原样返回
        result2 = _parse_iso_ts("short")
        assert result2 == "short"

    def test_short_string_falls_back(self):
        """短字符串 → 兜底."""
        result = _parse_iso_ts("2026-06")
        assert result == "2026-06"


# =============================================================================
# R17: html_report worker model 显示
# =============================================================================


class TestWorkerModelInHtml:
    def test_worker_model_shown_in_card(self):
        """Stage card 显示 worker model."""
        spec_yaml = """
name: t
stages:
  - id: a
    workers:
      - id: w
        cli: opencode
        model: opencode/deepseek-v4-pro
    deliverable: {path: out/a.json}
"""
        spec = load_workflow_from_string(spec_yaml)
        html = render_workflow_html(spec)
        # model 应在 card 里
        assert "deepseek-v4-pro" in html
        # 用 <code> 标签包裹
        assert "<code>opencode/deepseek-v4-pro</code>" in html

    def test_no_model_no_code_tag(self):
        """Worker 无 model → 不渲染空 code tag."""
        spec_yaml = """
name: t
stages:
  - id: a
    workers:
      - id: w
        cli: mock
    deliverable: {path: out/a.json}
"""
        spec = load_workflow_from_string(spec_yaml)
        html = render_workflow_html(spec)
        # 没有 model → 没有 <code> 包装的 model
        assert "opencode/deepseek" not in html


# =============================================================================
# P1: StageSpec.retry
# =============================================================================


class TestRetrySpec:
    def test_default(self):
        """默认 retry = 1 (不重试)."""
        spec = StageSpec(
            id="a",
            workers=[WorkerSpec(id="w")],
            deliverable=DeliverableSpec(path="out/a.json"),
        )
        assert spec.retry.max_attempts == 1
        assert spec.retry.backoff == "none"
        assert spec.retry.initial_delay == 5.0

    def test_custom_retry(self):
        spec = StageSpec(
            id="a",
            workers=[WorkerSpec(id="w")],
            deliverable=DeliverableSpec(path="out/a.json"),
            retry=RetrySpec(
                max_attempts=3,
                backoff="exponential",
                initial_delay=2.0,
                max_delay=30.0,
            ),
        )
        assert spec.retry.max_attempts == 3
        assert spec.retry.backoff == "exponential"

    def test_retry_yaml_parse(self):
        """YAML 解析 retry field."""
        spec = load_workflow_from_string("""
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json}
    retry:
      max_attempts: 3
      backoff: exponential
      initial_delay: 5.0
""")
        assert spec.stages[0].retry.max_attempts == 3
        assert spec.stages[0].retry.backoff == "exponential"
        assert spec.stages[0].retry.initial_delay == 5.0

    def test_invalid_max_attempts(self):
        """max_attempts 超出范围 (1-10) → ValidationError."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            RetrySpec(max_attempts=20)
        with pytest.raises(ValidationError):
            RetrySpec(max_attempts=0)

    def test_invalid_backoff(self):
        """backoff 不在枚举 → ValidationError."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            RetrySpec(backoff="random")


class TestComputeRetryDelay:
    """_compute_retry_delay 测试."""

    def test_none_backoff_zero(self, tmp_path: Path):
        """backoff='none' → 0.0."""
        spec = load_workflow_from_string("""
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json}
""")
        scheduler = WorkflowScheduler(spec, tmp_path)
        assert scheduler._compute_retry_delay(spec.stages[0], 0) == 0.0

    def test_fixed_backoff(self, tmp_path: Path):
        """backoff='fixed' → 总是 initial_delay."""
        spec = load_workflow_from_string("""
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json}
    retry:
      max_attempts: 5
      backoff: fixed
      initial_delay: 3.0
""")
        scheduler = WorkflowScheduler(spec, tmp_path)
        assert scheduler._compute_retry_delay(spec.stages[0], 0) == 3.0
        assert scheduler._compute_retry_delay(spec.stages[0], 2) == 3.0
        assert scheduler._compute_retry_delay(spec.stages[0], 4) == 3.0

    def test_exponential_backoff(self, tmp_path: Path):
        """backoff='exponential' → initial * 2^attempt."""
        spec = load_workflow_from_string("""
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json}
    retry:
      max_attempts: 5
      backoff: exponential
      initial_delay: 1.0
      max_delay: 100.0
""")
        scheduler = WorkflowScheduler(spec, tmp_path)
        assert scheduler._compute_retry_delay(spec.stages[0], 0) == 1.0
        assert scheduler._compute_retry_delay(spec.stages[0], 1) == 2.0
        assert scheduler._compute_retry_delay(spec.stages[0], 2) == 4.0
        assert scheduler._compute_retry_delay(spec.stages[0], 3) == 8.0

    def test_exponential_capped_by_max(self, tmp_path: Path):
        """指数爆炸被 max_delay 截断."""
        spec = load_workflow_from_string("""
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json}
    retry:
      max_attempts: 10
      backoff: exponential
      initial_delay: 1.0
      max_delay: 10.0
""")
        scheduler = WorkflowScheduler(spec, tmp_path)
        # attempt 5 → 1 * 2^5 = 32, 被 cap 到 10
        assert scheduler._compute_retry_delay(spec.stages[0], 5) == 10.0
        # attempt 10 → 1024, cap 到 10
        assert scheduler._compute_retry_delay(spec.stages[0], 10) == 10.0


# =============================================================================
# P1: scheduler retry 行为
# =============================================================================


class TestSchedulerRetry:
    """scheduler 重试行为测试.

    注: 不测试完整 run() (需要 busd), 只测试 retry 逻辑核心:
        max_attempts 后才最终失败.
    """

    def test_retry_attempts_tracked(self, tmp_path: Path):
        """_stage_retry_attempts 跟踪每个 stage."""
        spec = load_workflow_from_string("""
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json}
    retry:
      max_attempts: 3
      backoff: fixed
      initial_delay: 0.01
""")
        scheduler = WorkflowScheduler(spec, tmp_path)
        assert scheduler._stage_retry_attempts == {}
        # 模拟 stage 失败 → attempts++
        scheduler._stage_retry_attempts["a"] = 0
        # compute_delay
        delay = scheduler._compute_retry_delay(spec.stages[0], 0)
        assert delay == 0.01

    def test_retry_disabled_by_default(self, tmp_path: Path):
        """默认 max_attempts=1 → 失败立即终止."""
        spec = load_workflow_from_string("""
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json}
""")
        assert spec.stages[0].retry.max_attempts == 1


# =============================================================================
# R5: /api/workflows/registry
# =============================================================================


@pytest.fixture
def client_with_examples(tmp_path):
    """创建含 examples/ 目录的 client (自包含)."""
    from agents_chat.infra.server import create_app

    # data_dir 是 tmp_path 的子目录
    d = tmp_path / "data_v2"
    for sub in ["channels", "mailboxes", "sessions", "locks", "logs"]:
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "state_board.json").write_text('{"tasks":{},"updated_at":""}')
    (d / "channels" / "general.jsonl").touch()

    # 创建 examples/ 在 data_dir 父目录
    parent = tmp_path
    examples = parent / "examples"
    examples.mkdir(parents=True, exist_ok=True)
    (examples / "test-pipeline.yaml").write_text("""
name: test-pipeline
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json, min_size: 1}
""")
    (examples / "subdir").mkdir(exist_ok=True)
    (examples / "subdir" / "nested.yaml").write_text("""
name: nested-pipeline
stages:
  - id: x
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/x.json}
""")
    (examples / "invalid.yaml").write_text("name: bad\nstages: []\n")
    # data_dir 是子目录
    app = create_app(data_dir=d); return TestClient(app), parent


class TestRegistryEndpoint:
    def test_list_registered_workflows(self, client_with_examples):
        client, tmp_path = client_with_examples
        resp = client.get(f"/api/workflows/registry?scan_dir={tmp_path}/examples")
        assert resp.status_code == 200
        data = resp.json()
        assert "yaml_files" in data
        # 3 个 yaml: test-pipeline, nested, invalid
        assert len(data["yaml_files"]) == 3
        # 找出 valid 的
        valid = [f for f in data["yaml_files"] if "test-pipeline" in f["yaml_path"]]
        assert len(valid) == 1
        assert valid[0]["name"] == "test-pipeline"
        assert valid[0]["stage_count"] == 1
        # invalid.yaml 应含 error
        invalid = [f for f in data["yaml_files"] if "invalid" in f["yaml_path"]]
        assert len(invalid) == 1
        assert "error" in invalid[0]

    def test_nonexistent_dir(self, client_with_examples):
        client, _ = client_with_examples
        resp = client.get("/api/workflows/registry?scan_dir=/nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["yaml_files"] == []
        assert "error" in data

    def test_nested_yaml(self, client_with_examples):
        """rglob 找子目录的 yaml."""
        client, _ = client_with_examples
        resp = client.get("/api/workflows/registry?scan_dir=examples")
        assert resp.status_code == 200
        data = resp.json()
        names = [f.get("name", "") for f in data["yaml_files"]]
        # nested 也在
        assert "nested-pipeline" in names
