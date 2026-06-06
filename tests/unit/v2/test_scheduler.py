"""Integration tests for v2.0 Scheduler (超时 + 锁释放 + status_request)."""
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from agents_chat.v2.scheduler import Scheduler
from agents_chat.v2.files.lock import acquire


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestSchedulerBasic:
    @pytest.mark.asyncio
    async def test_no_stale_no_action(self, tmp_path):
        s = Scheduler(tmp_path, stale_ttl=300, check_interval=0.1)
        await s._check_once()
        # 没 stale, 没动作
        assert s.request_log == {}

    @pytest.mark.asyncio
    async def test_stale_first_request_status(self, tmp_path):
        """第一次发现 stale → 发 request_status 邮件 + 写频道."""
        s = Scheduler(tmp_path, stale_ttl=60, grace_period=10, check_interval=0.1)
        # 注册 agent
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        # 写入一个 stale task (heartbeat 100s 前)
        old_hb = _iso(datetime.now(timezone.utc) - timedelta(seconds=100))
        s.state_board.claim("task_stale_1", "qwencode", "local_001", channel="general", ref_msg_id="ch_1")
        s.state_board.update_from_status("task_stale_1", {"progress": 50}, agent_id="qwencode")
        # 手动把 heartbeat 改旧
        all_tasks = s.state_board.list_all()
        all_tasks["task_stale_1"]["heartbeat"] = old_hb
        s2 = Scheduler(tmp_path, stale_ttl=60, grace_period=10)
        # 重建时 state_file 还没存, request_log 空
        s2.state_board.update_from_status("task_stale_1", {"progress": 50}, agent_id="qwencode")
        all_tasks = s2.state_board.list_all()
        all_tasks["task_stale_1"]["heartbeat"] = old_hb
        import json
        (tmp_path / "state_board.json").write_text(json.dumps(all_tasks))
        # 重启, 加载
        s3 = Scheduler(tmp_path, stale_ttl=60, grace_period=10)
        await s3._check_once()
        # request_status 邮件应该投到 qwencode
        mb = s3.mailbox_of("qwencode")
        pending = mb.peek()
        assert any(m["type"] == "request_status" for m in pending)
        # 频道应该有 scheduler 通知
        ch = s3.channel("general")
        msgs = ch.tail(5)
        assert any("scheduler" in m.get("from", "") for m in msgs)
        # request_log 应该记录
        assert "task_stale_1" in s3.request_log

    @pytest.mark.asyncio
    async def test_stale_second_force_release(self, tmp_path):
        """第二次发现 stale (过 grace_period) → 强制释放锁 + 移除 state_board."""
        s = Scheduler(tmp_path, stale_ttl=60, grace_period=1, check_interval=0.1)
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        # 制造 stale task
        s.state_board.claim("task_stale_2", "qwencode", "local_001", channel="general", ref_msg_id="ch_1")
        # 改 heartbeat 为旧
        all_tasks = s.state_board.list_all()
        all_tasks["task_stale_2"]["heartbeat"] = _iso(datetime.now(timezone.utc) - timedelta(seconds=100))
        import json
        (tmp_path / "state_board.json").write_text(json.dumps(all_tasks))
        # 重建 (load fresh state)
        s2 = Scheduler(tmp_path, stale_ttl=60, grace_period=1)
        # 制造锁
        lock_path = tmp_path / "locks" / "task_task_stale_2.lock"
        acquire(lock_path, "qwencode", ttl_seconds=3600)
        assert lock_path.exists()
        # 第一次: 发 request_status
        await s2._check_once()
        assert "task_stale_2" in s2.request_log
        # 假装 grace_period 已过
        old_request_time = _iso(datetime.now(timezone.utc) - timedelta(seconds=10))
        s2.request_log["task_stale_2"] = old_request_time
        s2._save_request_log()
        # 第二次: 强制释放
        await s2._check_once()
        # 锁被删
        assert not lock_path.exists()
        # state_board entry 被删
        assert s2.state_board.get("task_stale_2") is None
        # request_log 清理
        assert "task_stale_2" not in s2.request_log

    @pytest.mark.asyncio
    async def test_stale_missing_agent_skips(self, tmp_path):
        """agent 不存在 (没 mailbox) → request_status 跳过."""
        s = Scheduler(tmp_path, stale_ttl=60, grace_period=10)
        s.state_board.claim("task_orphan", "ghost_agent", "local_001", channel="general")
        # heartbeat 改旧
        all_tasks = s.state_board.list_all()
        all_tasks["task_orphan"]["heartbeat"] = _iso(datetime.now(timezone.utc) - timedelta(seconds=100))
        import json
        (tmp_path / "state_board.json").write_text(json.dumps(all_tasks))
        s2 = Scheduler(tmp_path, stale_ttl=60, grace_period=10)
        await s2._check_once()
        # 不应 crash, request_log 应记录
        assert "task_orphan" in s2.request_log

    @pytest.mark.asyncio
    async def test_run_loop(self, tmp_path):
        """run() 主循环跑起来后, 制造 stale task 会被自动处理."""
        s = Scheduler(tmp_path, stale_ttl=10, grace_period=5, check_interval=0.1)
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        s.state_board.claim("task_run_test", "qwencode", "local_001", channel="general")
        # heartbeat 改旧
        all_tasks = s.state_board.list_all()
        all_tasks["task_run_test"]["heartbeat"] = _iso(datetime.now(timezone.utc) - timedelta(seconds=100))
        import json
        (tmp_path / "state_board.json").write_text(json.dumps(all_tasks))
        # 重新构造 s 加载新 state
        s = Scheduler(tmp_path, stale_ttl=10, grace_period=5, check_interval=0.1)
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.5)
        s.stop()
        await asyncio.wait_for(task, timeout=2.0)
        # request_status 邮件应该投到 qwencode
        mb = s.mailbox_of("qwencode")
        assert any(m["type"] == "request_status" for m in mb.peek())
