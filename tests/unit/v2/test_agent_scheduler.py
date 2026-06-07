"""独立 tests for v2.0 AgentScheduler (用 mock CLI + 真实 SessionManager)."""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from agents_chat.v2.agent_scheduler import (
    AgentScheduler,
    extract_mentions,
    derive_task_id,
)
from agents_chat.v2.cli.base import CLIResponse
from agents_chat.v2.cli.mock import MockCLI
from agents_chat.v2.communication import CommunicationComponent
from agents_chat.v2.files.channel import Channel
from agents_chat.v2.files.mailbox import Mailbox
from agents_chat.v2.session_manager import SessionManager
from agents_chat.v2.state_board import StateBoard


class MockCLIAlwaysOK:
    """测试用: 永远返回 OK + 含 STATUS 块."""
    name = "mock_ok"

    def __init__(self, output_text: str = "ok", progress: int = 50):
        self.output_text = output_text
        self.progress = progress
        self.calls = []

    async def execute(self, session_id, prompt, workspace_dir=""):
        self.calls.append({"session_id": session_id, "prompt": prompt, "ws": workspace_dir})
        return CLIResponse(
            output_text=f"{self.output_text}\n\n<!--STATUS\n session_id: {session_id or 'new'}\n task_id: t\n progress: {self.progress}\n summary: done\n next_action: wait\n confidence: high\n-->",
            new_session_id=f"qwen_{len(self.calls)}" if not session_id else None,
            elapsed_ms=10,
        )


class MockCLIFails:
    name = "mock_fail"

    async def execute(self, session_id, prompt, workspace_dir=""):
        return CLIResponse(output_text="", error="network down", elapsed_ms=10)


@pytest.fixture
def env(tmp_path):
    """完整测试环境: comms + sessions + cli + scheduler + 频道 + state_board."""
    mailbox = Mailbox(tmp_path / "mailboxes" / "agent1.json", "agent1")
    # 给其他可能 agent 的 mailbox 也建 (second-route 测试)
    (tmp_path / "mailboxes").mkdir(parents=True, exist_ok=True)
    Mailbox(tmp_path / "mailboxes" / "agent2.json", "agent2")
    Mailbox(tmp_path / "mailboxes" / "agent3.json", "agent3")
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir(parents=True, exist_ok=True)
    state_board = StateBoard(tmp_path / "state_board.json")
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    sessions = SessionManager(tmp_path / "sessions" / "agent1.json", "agent1")
    comms = CommunicationComponent(
        agent_id="agent1", mailbox=mailbox,
        channels_dir=channels_dir, state_board=state_board,
        lock_dir=lock_dir, default_channel="general",
        poll_interval=0.05,
    )
    workspace = tmp_path / "workspaces" / "agent1"
    workspace.mkdir(parents=True, exist_ok=True)
    cli = MockCLIAlwaysOK(output_text="我 100 元", progress=10)
    scheduler = AgentScheduler(
        comms=comms, sessions=sessions, cli=cli,
        agent_id="agent1", system_prompt="你是 agent1",
        workspace_dir=workspace, default_channel="general",
        channels_dir=channels_dir, lock_dir=lock_dir,
    )
    return {
        "tmp": tmp_path, "mailbox": mailbox, "channels_dir": channels_dir,
        "state_board": state_board, "lock_dir": lock_dir, "sessions": sessions,
        "comms": comms, "cli": cli, "scheduler": scheduler, "workspace": workspace,
    }


class TestSchedulerHelpers:
    def test_extract_mentions(self):
        assert extract_mentions("@alice @bob") == ["alice", "bob"]
        assert extract_mentions("@alice @bob @alice") == ["alice", "bob"]

    def test_extract_mentions_excludes_self(self, env):
        """scheduler 调用 extract_mentions 后自己过滤 self, 这里只测提取."""
        mentions = extract_mentions("@agent1 @agent2")
        assert "agent1" in mentions  # extract 提所有
        assert "agent2" in mentions
        # scheduler 的 _second_route 会 filter

    def test_derive_task_id_explicit(self):
        assert derive_task_id("[TASK task_abc] do x", "ch_1") == "task_abc"

    def test_derive_task_id_from_ref(self):
        assert derive_task_id("plain", "ch_5") == "task_ch_5"

    def test_derive_task_id_hash(self):
        tid = derive_task_id("plain")
        assert tid.startswith("task_auto_")


class TestSchedulerHandleMail:
    @pytest.mark.asyncio
    async def test_first_mail_creates_session(self, env):
        """第一封 mail → 新建 session, 调 CLI, 写频道."""
        env["mailbox"].append(
            ref_msg_id="ch_1", type="mention",
            content="@agent1 你好, 报个价", channel="general",
        )
        await env["scheduler"].handle_mail(env["mailbox"].read_and_clear()[0])
        # session 创建
        s_list = env["sessions"].list_all()
        assert len(s_list) == 1
        assert s_list[0].topic == "你好, 报个价"  # _extract_topic
        # CLI 被调
        assert len(env["cli"].calls) == 1
        # session.remote_id 被设置
        assert s_list[0].remote_id == "qwen_1"
        # 频道有 reply
        ch = Channel(env["channels_dir"] / "general.jsonl", "general")
        msgs = ch.tail(5)
        reply_msgs = [m for m in msgs if m["from"] == "agent1"]
        assert len(reply_msgs) == 1
        assert "100 元" in reply_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_second_mail_resumes_session(self, env):
        """第二封 mail 同 task + 同 topic → 续 session, 传 remote_id."""
        # 第一次
        env["mailbox"].append(
            ref_msg_id="ch_1", type="mention", content="@agent1 你好鱼市", channel="general",
        )
        await env["scheduler"].handle_mail(env["mailbox"].read_and_clear()[0])
        s = env["sessions"].list_all()[0]
        first_remote = s.remote_id

        # 第二次同 channel + 同 topic (让 fuzzy 命中)
        env["mailbox"].append(
            ref_msg_id="ch_2", type="mention", content="@agent1 你好鱼市", channel="general",
        )
        await env["scheduler"].handle_mail(env["mailbox"].read_and_clear()[0])
        # 还是 1 个 session
        assert len(env["sessions"].list_all()) == 1
        # CLI 第二次调用, session_id 传了 remote_id
        assert env["cli"].calls[1]["session_id"] == first_remote

    @pytest.mark.asyncio
    async def test_cli_error_writes_status_block(self, env):
        """CLI 失败: 写错误消息 + STATUS 块 (progress=0, confidence=low)."""
        env["cli"] = MockCLIFails()
        env["scheduler"].cli = env["cli"]
        env["mailbox"].append(
            ref_msg_id="ch_1", type="mention", content="@agent1 hi", channel="general",
        )
        await env["scheduler"].handle_mail(env["mailbox"].read_and_clear()[0])
        # 频道有错误消息
        ch = Channel(env["channels_dir"] / "general.jsonl", "general")
        msgs = ch.tail(5)
        err_msgs = [m for m in msgs if "CLI 错误" in m.get("content", "")]
        assert len(err_msgs) == 1
        assert "network down" in err_msgs[0]["content"]
        assert "confidence: low" in err_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_status_block_updates_session(self, env):
        """LLM reply 含 STATUS 块 → session 更新 progress/next_action/content_summary."""
        env["mailbox"].append(
            ref_msg_id="ch_1", type="mention", content="@agent1 hi", channel="general",
        )
        await env["scheduler"].handle_mail(env["mailbox"].read_and_clear()[0])
        s = env["sessions"].list_all()[0]
        assert s.progress == 10
        assert s.next_action == "wait"
        assert "done" in s.content_summary

    @pytest.mark.asyncio
    async def test_progress_100_marks_completed(self, env):
        env["cli"] = MockCLIAlwaysOK(output_text="ok", progress=100)
        env["scheduler"].cli = env["cli"]
        env["mailbox"].append(
            ref_msg_id="ch_1", type="mention", content="@agent1 hi", channel="general",
        )
        await env["scheduler"].handle_mail(env["mailbox"].read_and_clear()[0])
        s = env["sessions"].list_all()[0]
        assert s.progress == 100
        assert s.status == "completed"

    @pytest.mark.asyncio
    async def test_reply_routes_mentions(self, env):
        """reply 里有 @agent2 → 投递 mention 邮件到 agent2."""
        env["cli"] = MockCLIAlwaysOK(output_text="@agent2 看一下", progress=10)
        env["scheduler"].cli = env["cli"]
        env["mailbox"].append(
            ref_msg_id="ch_1", type="mention", content="@agent1 hi", channel="general",
        )
        await env["scheduler"].handle_mail(env["mailbox"].read_and_clear()[0])
        # agent2 邮箱有 mention
        agent2_mb = Mailbox(env["tmp"] / "mailboxes" / "agent2.json", "agent2")
        pending = agent2_mb.peek()
        assert len(pending) == 1
        assert pending[0]["type"] == "mention"
        # Mailbox.append 把 extra 合并到 msg, 所以 task_id 在顶层
        assert "task" in pending[0].get("task_id", "")

    @pytest.mark.asyncio
    async def test_reply_routes_skips_nonexistent(self, env):
        """reply 里有 @ghost (没 mailbox) → 静默 skip."""
        env["cli"] = MockCLIAlwaysOK(output_text="@ghost hi", progress=10)
        env["scheduler"].cli = env["cli"]
        env["mailbox"].append(
            ref_msg_id="ch_1", type="mention", content="@agent1 hi", channel="general",
        )
        # 不应该 crash
        await env["scheduler"].handle_mail(env["mailbox"].read_and_clear()[0])


class TestSchedulerHandleStale:
    @pytest.mark.asyncio
    async def test_handle_stale_with_session(self, env):
        """stale task 有关联 session → 调 CLI 重新生成 STATUS."""
        # 制造 stale task + session
        env["state_board"].claim("t-stale", "agent1", "s1", channel="general")
        # 用 sessions 关联到 task
        env["sessions"].create(topic="t-stale-task", channel="general", task_id="t-stale")
        # 跑 handle
        await env["scheduler"].handle_stale_task({"task_id": "t-stale", "channel": "general"})
        # CLI 被调
        assert len(env["cli"].calls) == 1
        # 频道有 status_report
        ch = Channel(env["channels_dir"] / "general.jsonl", "general")
        msgs = ch.tail(5)
        sr = [m for m in msgs if m.get("type") == "status_report"]
        assert len(sr) == 1

    @pytest.mark.asyncio
    async def test_handle_stale_no_session(self, env):
        """stale task 无 session → 写 default status_report."""
        await env["scheduler"].handle_stale_task({"task_id": "t-orphan", "channel": "general"})
        # 频道有 status_report
        ch = Channel(env["channels_dir"] / "general.jsonl", "general")
        msgs = ch.tail(5)
        sr = [m for m in msgs if m.get("type") == "status_report"]
        assert len(sr) == 1
        assert "stale" in sr[0]["content"]


class TestSchedulerRun:
    @pytest.mark.asyncio
    async def test_run_processes_incoming_mail(self, env):
        """run() 听 comms, 收到 mail 后处理."""
        async def push_mail():
            await asyncio.sleep(0.1)
            env["mailbox"].append(
                ref_msg_id="ch_1", type="mention",
                content="@agent1 hi", channel="general",
            )
        asyncio.create_task(push_mail())

        # 跑 scheduler, 收集一段时间
        async def stop_after():
            await asyncio.sleep(0.5)
            env["comms"].stop()
        asyncio.create_task(stop_after())

        await asyncio.wait_for(env["scheduler"].run(), timeout=2.0)
        # 验证 session 创建
        assert len(env["sessions"].list_all()) == 1
        # CLI 被调
        assert len(env["cli"].calls) == 1
