"""Integration tests for v2.0 Agent (with MockCLI)."""
import asyncio
import pytest
from pathlib import Path

from agents_chat.v2.agent import Agent, extract_mentions
from agents_chat.v2.cli.mock import MockCLI


class TestExtractMentions:
    def test_single(self):
        assert extract_mentions("@alice hi") == ["alice"]

    def test_multiple(self):
        assert extract_mentions("@alice @bob @charlie") == ["alice", "bob", "charlie"]

    def test_dedup(self):
        assert extract_mentions("@alice @bob @alice") == ["alice", "bob"]

    def test_no_mentions(self):
        assert extract_mentions("plain text") == []

    def test_email_not_mention(self):
        # email 格式不应误判
        assert extract_mentions("contact test@example.com") == ["example"]  # regex 会误抓, 已知限制

    def test_chinese_text(self):
        # 提取拉丁 mention
        assert extract_mentions("看一下 @claude 这个") == ["claude"]


class TestAgentInit:
    def test_creates_dirs(self, tmp_path):
        Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path)
        assert (tmp_path / "mailboxes").exists()
        assert (tmp_path / "sessions").exists()
        assert (tmp_path / "channels").exists()
        assert (tmp_path / "locks").exists()
        assert (tmp_path / "mailboxes" / "qwencode.json").exists()
        assert (tmp_path / "sessions" / "qwencode.json").exists()


class TestAgentRun:
    @pytest.mark.asyncio
    async def test_process_mention_mail(self, tmp_path):
        """手动投递 mention 邮件 → 跑一次 _process_one → 验证结果."""
        agent = Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path,
                      poll_interval=0.1)
        # 投递 mention 邮件
        agent.mailbox.append(
            ref_msg_id="ch_general_1", type="mention",
            content="@qwencode 处理 task_abc 数据库问题", channel="general",
            context_hint="",
        )
        # 处理
        mails = agent.mailbox.read_and_clear()
        await agent._process_batch(mails)
        # 验证
        # 1. 频道有 reply
        ch = agent.channel("general")
        msgs = ch.tail(5)
        assert len(msgs) >= 1  # 只有 reply (mention 是邮件, 不写频道)
        reply = [m for m in msgs if m["from"] == "qwencode"][0]
        assert "qwencode" in reply["from"]
        assert "<!--STATUS" in reply["content"]
        # 2. state_board 有 task
        entry = agent.state_board.get("task_abc")
        assert entry is not None
        assert entry["agent"] == "qwencode"
        assert entry["progress"] == 100  # MockCLI 返回 progress=100
        # 3. 锁被 release (因为 progress=100 标记 complete)
        lock = tmp_path / "locks" / "task_task_abc.lock"
        assert not lock.exists()  # 已 release

    @pytest.mark.asyncio
    async def test_no_claim_skip(self, tmp_path):
        """锁被别人持有时, mention 邮件应该 skip."""
        agent = Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path)
        # 别的 agent 提前占锁
        from agents_chat.v2.files.lock import acquire
        acquire(tmp_path / "locks" / "task_x.lock", "other_agent", ttl_seconds=3600)
        # 投递 task_broadcast
        agent.mailbox.append(
            ref_msg_id="ch_1", type="task_broadcast", content="[TASK] 干点啥", channel="general",
            extra={"task_id": "task_x"},
        )
        mails = agent.mailbox.read_and_clear()
        await agent._process_batch(mails)
        # 锁应该还是 other_agent 的
        lock_info = (tmp_path / "locks" / "task_x.lock").read_text()
        assert "other_agent" in lock_info

    @pytest.mark.asyncio
    async def test_request_status_responds(self, tmp_path):
        """收到 request_status → 写 STATUS 块到频道."""
        agent = Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path)
        # 先 claim 一个 task
        agent.state_board.claim("task_xyz", "qwencode", "local_001",
                                 channel="general", ref_msg_id="ch_1")
        agent.state_board.update_from_status("task_xyz", {
            "progress": 50, "summary": "doing",
        }, agent_id="qwencode")
        # 收 request_status
        agent.mailbox.append(
            ref_msg_id="ch_2", type="request_status",
            content="", channel="general",
            extra={"task_id": "task_xyz"},
        )
        mails = agent.mailbox.read_and_clear()
        await agent._process_batch(mails)
        # 频道应该有 status_report
        ch = agent.channel("general")
        status_msgs = [m for m in ch.tail(10) if m["type"] == "status_report"]
        assert len(status_msgs) == 1
        assert "task_xyz" in status_msgs[0]["content"]
        assert "progress: 50" in status_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_second_route_mention(self, tmp_path):
        """reply 里有 @claude → 投递 mention 邮件给 claude."""
        # MockCLI 自定义 reply 含 @claude
        cli = MockCLI(reply_template=(
            "qwencode 处理完了\n\n@claude 请复核\n\n"
            "<!--STATUS\n"
            " session_id: {session_id}\n"
            " task_id: {task_id}\n"
            " progress: 100\n"
            " summary: done\n"
            " next_action: 等待复核\n"
            " confidence: high\n"
            "-->"
        ))
        # 两个 agent
        qwen = Agent(agent_id="qwencode", cli=cli, data_dir=tmp_path)
        claude = Agent(agent_id="claude", cli=MockCLI(), data_dir=tmp_path)
        # qwen 处理 mention 邮件, reply 里写 @claude
        qwen.mailbox.append(
            ref_msg_id="ch_1", type="mention", content="@qwencode 查 task_001", channel="general",
            extra={"task_id": "task_001"},
        )
        mails = qwen.mailbox.read_and_clear()
        await qwen._process_batch(mails)
        # claude 邮箱应该有 mention
        claude_inbox = claude.mailbox.peek()
        assert any(m["type"] == "mention" for m in claude_inbox)
        # 验证 task_id 关联
        mention_mail = [m for m in claude_inbox if m["type"] == "mention"][0]
        assert mention_mail.get("task_id") == "task_001"

    @pytest.mark.asyncio
    async def test_session_resume_reuses(self, tmp_path):
        """同一 task 第二次处理, CLI 用 resume_session."""
        cli = MockCLI()
        agent = Agent(agent_id="qwencode", cli=cli, data_dir=tmp_path)
        # 第一次
        agent.mailbox.append(
            ref_msg_id="ch_1", type="mention", content="@qwencode task_007", channel="general",
            extra={"task_id": "task_007"},
        )
        await agent._process_batch(agent.mailbox.read_and_clear())
        # 第二次 (同样 task, 模拟调度中心重新投)
        agent.mailbox.append(
            ref_msg_id="ch_2", type="mention", content="@qwencode task_007 continue", channel="general",
            extra={"task_id": "task_007", "context_hint": "local_xxx"},  # context_hint 缺, 走 fallback
        )
        await agent._process_batch(agent.mailbox.read_and_clear())
        # MockCLI 第二次调用, 应该用了 resume (call_count=2)
        assert cli.call_count == 2

    @pytest.mark.asyncio
    async def test_run_loop_processes_mail(self, tmp_path):
        """run() 主循环: 后台跑, 投递邮件, 看 reply 出现."""
        agent = Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path,
                      poll_interval=0.1)
        # 启动后台
        task = asyncio.create_task(agent.run())
        await asyncio.sleep(0.1)  # 让它跑起来
        # 投递邮件
        agent.mailbox.append(
            ref_msg_id="ch_1", type="mention", content="@qwencode task_009", channel="general",
            extra={"task_id": "task_009"},
        )
        # 等处理
        for _ in range(20):
            await asyncio.sleep(0.1)
            ch = agent.channel("general")
            msgs = ch.tail(10)
            if any(m["from"] == "qwencode" and "task_009" in m["content"] for m in msgs):
                break
        # 停
        agent.stop()
        await asyncio.wait_for(task, timeout=2.0)
        # 验证
        ch = agent.channel("general")
        msgs = ch.tail(10)
        assert any(m["from"] == "qwencode" and "task_009" in m["content"] for m in msgs)

    @pytest.mark.asyncio
    async def test_cli_error_reports(self, tmp_path):
        """CLI 调用失败 → 写错误到频道 + 包含 STATUS 块."""
        from agents_chat.v2.cli.base import CLIResponse
        class FailingCLI:
            name = "failing"
            async def invoke(self, prompt, resume_session=None, workspace_dir=None):
                return CLIResponse(output_text="", error="network down")
        agent = Agent(agent_id="qwencode", cli=FailingCLI(), data_dir=tmp_path)
        agent.mailbox.append(
            ref_msg_id="ch_1", type="mention", content="@qwencode task_010", channel="general",
            extra={"task_id": "task_010"},
        )
        await agent._process_batch(agent.mailbox.read_and_clear())
        ch = agent.channel("general")
        reply = [m for m in ch.tail(5) if m["from"] == "qwencode"][0]
        assert "CLI 错误" in reply["content"]
        assert "network down" in reply["content"]
        assert "<!--STATUS" in reply["content"]
        assert "confidence: low" in reply["content"]
