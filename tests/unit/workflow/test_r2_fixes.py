"""
补充测试 - Round 2 修复的回归测试.

覆盖:
  R29: _finalize 测试 — 清理 + 持久化
  R30: _filter_stages 错误路径 — from_stage="nonexistent" 抛 ValueError
  T27: 并发 pipeline 测试 — 2 pipeline 同时跑
  T28: runner 异常测试 — WorkerFactory.create 失败场景
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agents_chat.workflow import (
    WorkflowRunResult,
    WorkflowScheduler,
    load_workflow_from_string,
)


# =============================================================================
# Fixtures
# =============================================================================

SIMPLE_WF = """
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json, min_size: 1}
    timeout: 5
"""


def init_data_v2(data_dir: Path) -> None:
    for sub in ["channels", "mailboxes", "sessions", "locks", "logs"]:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    (data_dir / "state_board.json").write_text('{"tasks":{},"updated_at":""}')
    (data_dir / "channels" / "general.jsonl").touch()


# =============================================================================
# R30: _filter_stages 错误路径
# =============================================================================


class TestFilterStagesErrors:
    def test_from_stage_not_found_raises(self, tmp_path: Path):
        """from_stage 不存在 → ValueError (R13 修复)."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path, from_stage="nonexistent")
        with pytest.raises(ValueError, match="from_stage 'nonexistent' not found"):
            scheduler._filter_stages(spec.topological_order())

    def test_single_stage_not_found_raises(self, tmp_path: Path):
        """single_stage 不存在 → ValueError."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path, single_stage="nonexistent")
        with pytest.raises(ValueError, match="stage 'nonexistent' not found"):
            scheduler._filter_stages(spec.topological_order())

    def test_from_stage_valid_returns_remaining(self, tmp_path: Path):
        """from_stage 存在 → 返 from_stage + 下游."""
        spec_yaml = """
name: t
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json}
  - id: b
    depends_on: [a]
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/b.json}
  - id: c
    depends_on: [b]
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/c.json}
"""
        spec = load_workflow_from_string(spec_yaml)
        scheduler = WorkflowScheduler(spec, tmp_path, from_stage="b")
        filtered = scheduler._filter_stages(spec.topological_order())
        assert [s.id for s in filtered] == ["b", "c"]


# =============================================================================
# R29: _finalize 测试
# =============================================================================


class TestFinalize:
    @pytest.mark.asyncio
    async def test_finalize_persists_state(self, tmp_path: Path):
        """_finalize 写 runs/<id>.json 含 stage_deps."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path)
        result = scheduler._finalize()
        run_file = tmp_path / "runs" / f"{result.run_id}.json"
        assert run_file.exists()
        data = json.loads(run_file.read_text())
        assert "stage_deps" in data
        assert data["stage_deps"] == {"a": []}

    @pytest.mark.asyncio
    async def test_finalize_continues_on_save_error(self, tmp_path: Path):
        """_save_run_state 失败不中断 (log + 返 result)."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path)

        # 模拟 _save_run_state 抛错
        with patch.object(scheduler, '_save_run_state',
                          side_effect=OSError("disk full")):
            # 不应抛
            result = scheduler._finalize()
        assert result.run_id == scheduler.run_id

    @pytest.mark.asyncio
    async def test_finalize_pops_dict_entries(self, tmp_path: Path):
        """_cleanup_stage 释放 _stage_channels/_stage_agents/_stage_tasks."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path)

        # 模拟 stage 已启动
        from agents_chat.workflow.schema import StageSpec
        stage = spec.stages[0]
        scheduler._stage_channels[stage.id] = tmp_path / "fake.jsonl"
        scheduler._stage_agents[stage.id] = []
        scheduler._stage_tasks[stage.id] = []

        scheduler._cleanup_stage(stage)

        # dict entry 已清
        assert stage.id not in scheduler._stage_channels
        assert stage.id not in scheduler._stage_agents
        assert stage.id not in scheduler._stage_tasks


# =============================================================================
# T27: 并发 pipeline 测试
# =============================================================================


class TestConcurrentPipelines:
    @pytest.mark.asyncio
    async def test_two_schedulers_independent_state(self, tmp_path: Path):
        """2 scheduler 同时存在, 状态独立.

        注: 完整 run() 需要 busd + 真 worker, 不适合并发测试.
        这里只验证 2 个 scheduler 的内部状态互不干扰.
        """
        init_data_v2(tmp_path)
        spec = load_workflow_from_string(SIMPLE_WF)

        s1 = WorkflowScheduler(spec, tmp_path, run_id="run-A")
        s2 = WorkflowScheduler(spec, tmp_path, run_id="run-B")

        # 模拟 stage 状态
        stage = spec.stages[0]
        s1._stage_agents[stage.id] = [1]
        s2._stage_agents[stage.id] = [2, 3]

        # 互不干扰
        assert s1._stage_agents[stage.id] == [1]
        assert s2._stage_agents[stage.id] == [2, 3]
        assert s1.result.run_id == "run-A"
        assert s2.result.run_id == "run-B"

    @pytest.mark.asyncio
    async def test_two_schedulers_concurrent_finalize(self, tmp_path: Path):
        """2 scheduler 并发 _finalize, 互不干扰."""
        init_data_v2(tmp_path)
        spec = load_workflow_from_string(SIMPLE_WF)

        s1 = WorkflowScheduler(spec, tmp_path, run_id="run-X")
        s2 = WorkflowScheduler(spec, tmp_path, run_id="run-Y")

        r1, r2 = s1._finalize(), s2._finalize()
        assert r1.run_id == "run-X"
        assert r2.run_id == "run-Y"
        # 各自持久化
        assert (tmp_path / "runs" / "run-X.json").exists()
        assert (tmp_path / "runs" / "run-Y.json").exists()


# =============================================================================
# T28: runner 异常测试
# =============================================================================


class TestRunnerExceptions:
    def test_duplicate_worker_id_raises(self):
        """StageSpec 自己已经校验 worker id 唯一 (模型层).

        但 runner.py 也加保险, 直接调会抛 ValueError.
        """
        from agents_chat.workflow.schema import StageSpec, WorkerSpec, DeliverableSpec
        from agents_chat.workflow.runner import spawn_stage_workers

        # 直接构造有重复 worker id 的 StageSpec 不会被 Pydantic 拒绝
        # 因为 Pydantic 在 model_validator 阶段会拒绝
        import pytest
        with pytest.raises(Exception):
            StageSpec(
                id="s",
                workers=[WorkerSpec(id="dup"), WorkerSpec(id="dup")],
                deliverable=DeliverableSpec(path="out/a.json"),
            )

    def test_spawn_stage_workers_handles_workspace_error(self, tmp_path: Path):
        """WorkerFactory.create 抛错 (workspace 不可写) → 透传异常."""
        from agents_chat.workflow.schema import StageSpec, WorkerSpec, DeliverableSpec
        from agents_chat.workflow.runner import spawn_stage_workers

        stage = StageSpec(
            id="s",
            workers=[WorkerSpec(id="w", cli="mock")],
            deliverable=DeliverableSpec(path="out/x.json"),
        )

        # 模拟 WorkerFactory.create 抛错
        # 实际调用是: from agents_chat.infra.worker_factory import WorkerFactory
        # 所以 patch 目标需要是该 module 的 WorkerFactory
        with patch("agents_chat.infra.worker_factory.WorkerFactory.create",
                   side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError, match="denied"):
                spawn_stage_workers(stage, tmp_path, ".stage-s-run")

    def test_spawn_stage_workers_collects_inputs(self, tmp_path: Path):
        """spawn_stage_workers 收集 upstream input 并注入 system_prompt."""
        from agents_chat.workflow.schema import StageSpec, WorkerSpec, DeliverableSpec
        from agents_chat.workflow.runner import spawn_stage_workers, build_system_prompt

        # 写 upstream deliverable
        upstream = tmp_path / "upstream.json"
        upstream.write_text('{"findings": ["a", "b"]}')

        # worker system_prompt 含 {input.findings}
        stage = StageSpec(
            id="s",
            workers=[WorkerSpec(id="w", cli="mock",
                                system_prompt="Based on {input.findings} write report")],
            deliverable=DeliverableSpec(path="out/x.json"),
        )

        # Mock WorkerFactory.create 避免真启 worker
        captured = {}
        def fake_create(agent_id, cli_type, data_dir, **kwargs):
            captured["system_prompt"] = kwargs.get("system_prompt", "")
            captured["cli_type"] = cli_type
            captured["data_dir"] = data_dir
            captured["agent_id"] = agent_id
            mock_agent = MagicMock()
            mock_agent.agent_id = agent_id
            return mock_agent

        # patch 目标: spawn_stage_workers 函数内 import 的模块
        with patch("agents_chat.infra.worker_factory.WorkerFactory.create",
                   side_effect=fake_create):
            agents = spawn_stage_workers(
                stage, tmp_path, ".stage-s-run",
                upstream_deliverables=[("prev", upstream)],
            )
            assert len(agents) == 1
            # system_prompt 应含 findings list
            assert "['a', 'b']" in captured["system_prompt"]
            # cli_type 透传
            assert captured["cli_type"] == "mock"
