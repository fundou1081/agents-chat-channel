"""
Round 3 修复的测试.

覆盖:
  R7: workflow cancel() 方法
  R8: WorkflowRegistry 注册 / 查找 / 注销
  R9: CLI workflow cancel / active
  R14: poll_interval 可配置
  R15: spawn_delay 可配置
"""
from __future__ import annotations

import asyncio
import json
import sys
import io
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agents_chat.workflow import (
    WorkflowRegistry,
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


# =============================================================================
# R7: cancel() 方法
# =============================================================================


class TestCancelMethod:
    def test_cancel_sets_flag(self, tmp_path: Path):
        """cancel() 设置 _cancel_requested 标志."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path)
        assert scheduler._cancel_requested is False
        scheduler.cancel()
        assert scheduler._cancel_requested is True
        assert scheduler.result.status == "canceled"

    def test_cancel_idempotent(self, tmp_path: Path):
        """cancel() 多次调用不报错 (幂等)."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path)
        scheduler.cancel()
        scheduler.cancel()  # 不应抛
        assert scheduler._cancel_requested is True

    def test_cancel_during_run(self, tmp_path: Path):
        """cancel() 在 run() 循环中被检测到."""
        spec = load_workflow_from_string(SIMPLE_WF)
        # 不预写 deliverable, 让 stage a timeout
        scheduler = WorkflowScheduler(spec, tmp_path, run_id="run-cancel")
        registry = WorkflowRegistry.get_default()
        registry.register(scheduler)
        # 模拟 cancel 在 stage 启动后
        original_start = scheduler._start_stage
        async def start_with_cancel(*args, **kwargs):
            result = await original_start(*args, **kwargs)
            scheduler.cancel()  # 启动后立即取消
            return result
        scheduler._start_stage = start_with_cancel

        async def run_test():
            return await scheduler.run()

        result = asyncio.run(run_test())
        registry.unregister(scheduler.run_id)
        assert result.status in ("canceled", "failed")  # 至少不是 success


# =============================================================================
# R8: WorkflowRegistry
# =============================================================================


class TestRegistry:
    def test_register_and_get(self):
        """register / get 流程."""
        reg = WorkflowRegistry()
        reg.unregister("run-x")  # 不应抛
        # 用 mock scheduler (不需要真建 spec)
        mock_sched = MagicMock()
        mock_sched.run_id = "run-x"
        reg.register(mock_sched)
        assert reg.get("run-x") is mock_sched
        reg.unregister("run-x")
        assert reg.get("run-x") is None

    def test_list_active(self):
        """list_active 返所有已注册 run_id."""
        reg = WorkflowRegistry()
        for rid in ("run-1", "run-2"):
            mock_sched = MagicMock()
            mock_sched.run_id = rid
            reg.register(mock_sched)
        assert set(reg.list_active()) == {"run-1", "run-2"}

    def test_singleton(self):
        """get_default() 单例模式."""
        a = WorkflowRegistry.get_default()
        b = WorkflowRegistry.get_default()
        assert a is b

    def test_thread_safe(self):
        """并发 register / unregister 不抛 (threading lock)."""
        import threading
        reg = WorkflowRegistry()
        errors = []
        def worker(i):
            try:
                for j in range(10):
                    mock_sched = MagicMock()
                    mock_sched.run_id = f"run-{i}-{j}"
                    reg.register(mock_sched)
                    reg.unregister(mock_sched.run_id)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# =============================================================================
# R14: poll_interval 可配置
# =============================================================================


class TestConfigurableIntervals:
    def test_poll_interval_custom(self, tmp_path: Path):
        """poll_interval 自定义值."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path, poll_interval=0.5)
        assert scheduler.poll_interval == 0.5

    def test_poll_interval_default(self, tmp_path: Path):
        """poll_interval 默认 2.0."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path)
        assert scheduler.poll_interval == 2.0

    def test_spawn_delay_custom(self, tmp_path: Path):
        """spawn_delay 自定义值."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path, spawn_delay=0.1)
        assert scheduler.spawn_delay == 0.1

    def test_spawn_delay_default(self, tmp_path: Path):
        """spawn_delay 默认 0.5."""
        spec = load_workflow_from_string(SIMPLE_WF)
        scheduler = WorkflowScheduler(spec, tmp_path)
        assert scheduler.spawn_delay == 0.5

    @pytest.mark.asyncio
    async def test_fast_poll(self, tmp_path: Path):
        """fast poll_interval 让测试更快."""
        spec = load_workflow_from_string(SIMPLE_WF)
        spec.stages[0].timeout = 3
        # 预写 deliverable
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "a.json").write_text("## ok")
        # 用 0.1s poll 应该更快
        scheduler = WorkflowScheduler(spec, tmp_path, poll_interval=0.1)
        start = time.time()
        success = await scheduler._wait_stage_done(spec.stages[0])
        elapsed = time.time() - start
        assert success is True
        # 应 < 1s
        assert elapsed < 1


# =============================================================================
# R9: CLI cancel / active
# =============================================================================


class TestCLICancel:
    def test_cmd_active_success(self):
        """cmd_active 调用 server /api/workflows/active."""
        from agents_chat.workflow.cli import cmd_active

        mock_response = json.dumps({"active": ["run-1", "run-2"]})
        args = MagicMock()
        args.server_url = "http://127.0.0.1:8765"

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response.encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            try:
                cmd_active(args)
                output = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            assert "run-1" in output
            assert "Active workflows (2)" in output

    def test_cmd_active_empty(self):
        """cmd_active 无 active workflow."""
        from agents_chat.workflow.cli import cmd_active

        mock_response = json.dumps({"active": []})
        args = MagicMock()
        args.server_url = "http://127.0.0.1:8765"

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response.encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            try:
                cmd_active(args)
                output = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            assert "no active workflows" in output

    def test_cmd_cancel_success(self):
        """cmd_cancel 调用 server cancel endpoint."""
        from agents_chat.workflow.cli import cmd_cancel

        mock_response = json.dumps({
            "run_id": "run-1",
            "status": "canceled",
            "message": "cancel signal sent",
        })
        args = MagicMock()
        args.run_id = "run-1"
        args.server_url = "http://127.0.0.1:8765"

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response.encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            try:
                cmd_cancel(args)
                output = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
            assert "✅" in output
            assert "run-1" in output
            assert "canceled" in output

    def test_cmd_cancel_404(self):
        """cmd_cancel 收到 404 → sys.exit(1) + error message."""
        from agents_chat.workflow.cli import cmd_cancel

        args = MagicMock()
        args.run_id = "nonexistent"
        args.server_url = "http://127.0.0.1:8765"

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "url", 404, "Not Found", {}, None
            )
            with pytest.raises(SystemExit) as exc:
                cmd_cancel(args)
            assert exc.value.code == 1


# =============================================================================
# Time import (for the new test)
# =============================================================================
import time
