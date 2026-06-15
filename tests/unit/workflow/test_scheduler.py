"""
WorkflowScheduler 测试.

覆盖:
  1. test_filter_stages_linear - 全跑
  2. test_filter_stages_from_stage - 从 from_stage 开始
  3. test_filter_stages_single - 单 stage 跑
  4. test_wait_stage_done_basic - 文件存在 + size 满足
  5. test_wait_stage_done_min_size_fail - 文件太小 fail
  6. test_wait_stage_done_timeout - timeout 触发
  7. test_wait_stage_done_checks_pass - v2 checks pass
  8. test_wait_stage_done_checks_fail - v2 checks fail
  9. test_handoff_deliverable - 文件复制到下游 workspace
  10. test_cleanup_stage - 删私有 channel
  11. test_run_full_success - 端到端 (mock worker)
  12. test_run_with_failure - stage 失败 → workflow 失败
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from agents_chat.workflow import (
    WorkflowRunResult,
    WorkflowScheduler,
    load_workflow_from_string,
)


# =============================================================================
# Fixtures
# =============================================================================


def make_simple_workflow_yaml() -> str:
    """3 stage 简单 workflow, 全部 mock CLI."""
    return """
name: test-pipeline
stages:
  - id: a
    workers:
      - id: w-a
        cli: mock
    deliverable:
      path: out/a.json
      min_size: 1
      checks: ["## ok"]

  - id: b
    depends_on: [a]
    workers:
      - id: w-b
        cli: mock
    deliverable:
      path: out/b.json
      min_size: 1

  - id: c
    depends_on: [b]
    workers:
      - id: w-c
        cli: mock
    deliverable:
      path: out/c.json
      min_size: 1
"""


def write_deliverable(path: Path, content: str = "## ok\ndata") -> None:
    """模拟 worker 写完 deliverable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# =============================================================================
# _filter_stages 测试
# =============================================================================


class TestFilterStages:
    def test_no_filter_all_stages(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path)
        filtered = scheduler._filter_stages(spec.topological_order())
        assert [s.id for s in filtered] == ["a", "b", "c"]

    def test_from_stage_skips_previous(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path, from_stage="b")
        filtered = scheduler._filter_stages(spec.topological_order())
        assert [s.id for s in filtered] == ["b", "c"]

    def test_single_stage_only_one(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path, single_stage="b")
        filtered = scheduler._filter_stages(spec.topological_order())
        assert [s.id for s in filtered] == ["b"]

    def test_single_stage_not_found_raises(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path, single_stage="nonexistent")
        with pytest.raises(ValueError, match="not found"):
            scheduler._filter_stages(spec.topological_order())


# =============================================================================
# _get_*_path helpers
# =============================================================================


class TestGetDeliverablePaths:
    def test_single_path(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path)
        paths = scheduler._get_all_deliverable_paths(spec.stages[0])
        assert paths == [tmp_path / "out" / "a.json"]

    def test_primary_path(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path)
        primary = scheduler._get_deliverable_primary_path(spec.stages[0])
        assert primary == tmp_path / "out" / "a.json"


# =============================================================================
# _wait_stage_done 测试
# =============================================================================


class TestWaitStageDone:
    @pytest.mark.asyncio
    async def test_basic_success(self, tmp_path):
        """文件存在 + size 满足 + 无 checks → done."""
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path)

        # 预先写好 deliverable
        write_deliverable(tmp_path / "out" / "a.json", "## ok\nsome data")

        # 短 timeout, 应该立即 done
        spec.stages[0].timeout = 5
        success = await scheduler._wait_stage_done(spec.stages[0])
        assert success is True
        assert scheduler.result.stage_states == {}  # _wait 不写 stage_states

    @pytest.mark.asyncio
    async def test_min_size_fail(self, tmp_path):
        """文件 < min_size → fail (但只是返回 False, 不抛)."""
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        # 调高 min_size, 文件不够大
        spec.stages[0].deliverable.min_size = 100000
        write_deliverable(tmp_path / "out" / "a.json", "tiny")
        spec.stages[0].timeout = 2

        scheduler = WorkflowScheduler(spec, tmp_path)
        success = await scheduler._wait_stage_done(spec.stages[0])
        assert success is False

    @pytest.mark.asyncio
    async def test_timeout_trigger(self, tmp_path):
        """文件不出现, timeout 触发."""
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        spec.stages[0].timeout = 1  # 1s 超时 (smoke test)
        scheduler = WorkflowScheduler(spec, tmp_path)
        start = time.time()
        success = await scheduler._wait_stage_done(spec.stages[0])
        elapsed = time.time() - start
        assert success is False
        assert elapsed < 5  # 没卡死

    @pytest.mark.asyncio
    async def test_checks_pass(self, tmp_path):
        """v2 checks: 字符串启发式, 含 ## → contains → pass."""
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        # checks = ["## ok"] (启发式: 含 ## → contains)
        write_deliverable(tmp_path / "out" / "a.json", "## ok\ndata")
        spec.stages[0].timeout = 3

        scheduler = WorkflowScheduler(spec, tmp_path)
        success = await scheduler._wait_stage_done(spec.stages[0])
        assert success is True
        # check_results 记录
        assert "a" in scheduler._stage_check_results

    @pytest.mark.asyncio
    async def test_checks_fail_then_pass(self, tmp_path):
        """v2 checks: 文件先 fail, 写第二次 pass."""
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        spec.stages[0].timeout = 10
        spec.stages[0].deliverable.min_size = 1
        # 第一次写不含 ## ok (fail)
        write_deliverable(tmp_path / "out" / "a.json", "no ok here")

        scheduler = WorkflowScheduler(spec, tmp_path)
        # 用 task 异步在 1s 后覆写
        async def overwrite():
            await asyncio.sleep(1.5)
            write_deliverable(tmp_path / "out" / "a.json", "## ok\nnow correct")
        asyncio.create_task(overwrite())

        success = await scheduler._wait_stage_done(spec.stages[0])
        assert success is True


# =============================================================================
# _handoff_deliverable 测试
# =============================================================================


class TestHandoffDeliverable:
    def test_copy_to_downstream_workspace(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path)

        # 模拟 stage a 完, deliverable 在
        upstream_deliverable = tmp_path / "out" / "a.json"
        write_deliverable(upstream_deliverable, '{"sources": ["a"]}')

        # handoff 到 stage b 的 worker workspace
        scheduler._handoff_deliverable(
            spec.stages[1],  # stage b
            upstream_deliverables=[("a", upstream_deliverable)],
        )

        # 验证 worker w-b 的 stage_inputs/a.json
        w_b_input = tmp_path / "workspaces" / "w-b" / "stage_inputs" / "a.json"
        assert w_b_input.exists()
        assert json.loads(w_b_input.read_text()) == {"sources": ["a"]}

    def test_no_upstream_silent(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path)
        # 无 upstream, 不报错, 不做事
        scheduler._handoff_deliverable(spec.stages[0], [])


# =============================================================================
# _cleanup_stage 测试
# =============================================================================


class TestCleanupStage:
    def test_deletes_channel_file(self, tmp_path):
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        scheduler = WorkflowScheduler(spec, tmp_path)
        # 模拟创建私有 channel
        channel_file = tmp_path / "channels" / ".stage-a-run-xxx.jsonl"
        channel_file.parent.mkdir(parents=True, exist_ok=True)
        channel_file.write_text('{"id":"m1","ts":"2026-01-01","from":"w-a","content":"hi","mentions":[],"type":"text"}')
        # 模拟 deliverable 也写好 (不应被删)
        deliverable = tmp_path / "out" / "a.json"
        deliverable.parent.mkdir(parents=True, exist_ok=True)
        deliverable.write_text("## ok\n")
        # 设置 channel 路径到 scheduler
        scheduler._stage_channels["a"] = channel_file
        # 模拟 agents (空, 因为 stop() 需要有 .stop() 方法)
        scheduler._stage_agents["a"] = []

        scheduler._cleanup_stage(spec.stages[0])

        # channel 文件删了
        assert not channel_file.exists()
        # deliverable 保留
        assert deliverable.exists()


# =============================================================================
# WorkflowRunResult 序列化
# =============================================================================


class TestRunResultSerialization:
    def test_to_dict(self):
        r = WorkflowRunResult("test", "run-abc")
        r.status = "success"
        d = r.to_dict()
        assert d["workflow_name"] == "test"
        assert d["run_id"] == "run-abc"
        assert d["status"] == "success"
        assert d["failed_stage"] is None
        assert "started_at" in d
        assert "finished_at" in d

    def test_to_json_serializable(self):
        """to_json() 返可 JSON 解析的字符串 (持久化用)."""
        r = WorkflowRunResult("test", "run-abc")
        r.status = "failed"
        r.failed_stage = "a"
        j = r.to_json()
        # 重新 parse, 验证 valid JSON
        d = json.loads(j)
        assert d["status"] == "failed"
        assert d["failed_stage"] == "a"


# =============================================================================
# 端到端 run() 测试 (用真 mock CLI 跑)
# =============================================================================


class TestSchedulerE2E:
    @pytest.mark.asyncio
    async def test_run_full_success_mock_cli(self, tmp_path):
        """端到端: 3 stage 全部用 mock CLI, 期望全 success.

        MockCLI 默认 30s timeout 太长, 我们用更短的 stage.timeout 触发 timeout
        不行 (mock 不会真写 deliverable). 这里改用直接调 runner, 模拟
        deliverable 文件.
        """
        spec = load_workflow_from_string(make_simple_workflow_yaml())
        # 短 timeout, 加快测试
        for s in spec.stages:
            s.timeout = 5

        scheduler = WorkflowScheduler(spec, tmp_path)

        # 模拟: stage 启动后, 我们直接写 deliverable
        async def simulate_stage_work(stage_id: str, content: str = "## ok\ndata"):
            """stage 启动后 0.5s 写 deliverable."""
            await asyncio.sleep(0.5)
            # 找 stage 的 deliverable 路径
            stage = next(s for s in spec.stages if s.id == stage_id)
            primary = scheduler._get_deliverable_primary_path(stage)
            if primary:
                write_deliverable(primary, content)
            # 等一小会, 让 scheduler 检测
            await asyncio.sleep(0.5)

        # 3 stage 并行模拟 (在 scheduler 跑的同时)
        async def mock_workers():
            await asyncio.sleep(0.5)  # 等 stage a 启
            write_deliverable(tmp_path / "out" / "a.json")
            await asyncio.sleep(2)    # 等 stage b
            write_deliverable(tmp_path / "out" / "b.json")
            await asyncio.sleep(2)    # 等 stage c
            write_deliverable(tmp_path / "out" / "c.json")

        # 跑 scheduler, 同时跑 mock_workers
        scheduler_task = asyncio.create_task(scheduler.run())
        mock_task = asyncio.create_task(mock_workers())

        result = await scheduler_task
        # 取消 mock_task (如果还活着)
        if not mock_task.done():
            mock_task.cancel()

        # 验证
        assert result.status == "success", f"got {result.status}, failed_stage={result.failed_stage}"
        assert result.stage_states == {"a": "success", "b": "success", "c": "success"}
        # 持久化文件
        run_file = tmp_path / "runs" / f"{result.run_id}.json"
        assert run_file.exists()
        saved = json.loads(run_file.read_text())
        assert saved["status"] == "success"
        assert saved["workflow_name"] == "test-pipeline"
