"""集成 tests for v2.0 Agent 容器 (4 组件组装)."""
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock

from agents_chat.core.agent import Agent
from agents_chat.infra.cli import CLIResponse
from agents_chat.infra.cli import MockCLI


class MockCLIAlwaysOK:
    """测试用 CLI: 永远 OK, 含 STATUS 块."""
    name = "mock_ok"
    def __init__(self, output="ok", progress=50, summary="done"):
        self.output = output
        self.progress = progress
        self.summary = summary
        self.calls = []

    async def execute(self, session_id, prompt, workspace_dir=""):
        self.calls.append({"session_id": session_id, "ws": workspace_dir})
        return CLIResponse(
            output_text=f"{self.output}\n\n<!--STATUS\n session_id: {session_id or 'new'}\n task_id: t\n progress: {self.progress}\n summary: {self.summary}\n next_action: wait\n confidence: high\n-->",
            new_session_id=f"qwen_{len(self.calls)}" if not session_id else None,
            elapsed_ms=10,
        )


@pytest.fixture
def env(tmp_path):
    cli = MockCLIAlwaysOK()
    agent = Agent(
        agent_id="qwencode", cli=cli, data_dir=tmp_path,
        poll_interval=0.1, default_channel="general",
    )
    return {"tmp": tmp_path, "agent": agent, "cli": cli}


class TestAgentInit:
    def test_creates_dirs(self, tmp_path):
        Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path)
        assert (tmp_path / "mailboxes").exists()
        assert (tmp_path / "channels").exists()
        assert (tmp_path / "locks").exists()
        assert (tmp_path / "sessions").exists()
        assert (tmp_path / "mailboxes" / "qwencode.json").exists()
        assert (tmp_path / "sessions" / "qwencode.json").exists()
        assert (tmp_path / "state_board.json").exists()

    def test_creates_workspace(self, tmp_path):
        Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path)
        ws = tmp_path / "workspaces" / "qwencode"
        assert ws.exists()
        assert (ws / "mock.md").exists()

    def test_custom_workspace(self, tmp_path):
        custom = tmp_path / "my_ws"
        agent = Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path, workspace_dir=custom)
        assert agent.workspace_dir == custom

    def test_4_components_wired(self, env):
        """4 组件 (comms + sessions + cli + scheduler) 都组装."""
        agent = env["agent"]
        assert agent.comms is not None
        assert agent.sessions is not None
        assert agent.cli is env["cli"]
        assert agent.scheduler is not None
        # scheduler 用 comms / sessions / cli
        assert agent.scheduler.comms is agent.comms
        assert agent.scheduler.sessions is agent.sessions
        assert agent.scheduler.cli is agent.cli
        assert agent.scheduler.agent_id == "qwencode"

    def test_backward_compat_helpers(self, env):
        """channel() / mailbox_of() / snapshot() 仍可用."""
        agent = env["agent"]
        ch = agent.channel("general")
        assert ch is not None
        mb = agent.mailbox_of("qwencode")
        assert mb is not None
        snap = agent.snapshot()
        assert snap["agent_id"] == "qwencode"
        assert "active_sessions" in snap

    def test_trigger_immediate_tick(self, env):
        """trigger_immediate_tick 委派给 comms.on_new_mail (不报错)."""
        agent = env["agent"]
        # 不应该抛错
        agent.trigger_immediate_tick()
        # comms 的 _new_mail_event 应被 set
        assert agent.comms._new_mail_event.is_set()


class TestAgentRun:
    @pytest.mark.asyncio
    async def test_run_processes_mail(self, env):
        """run() 跑通 + 收到 mail 后处理 (端到端)."""
        agent = env["agent"]
        env["cli"].output = "我 100 元"
        env["cli"].progress = 10

        async def push_mail():
            await asyncio.sleep(0.15)
            agent.mailbox.append(
                ref_msg_id="ch_1", type="mention",
                content="@qwencode 你好", channel="general",
            )
        asyncio.create_task(push_mail())

        async def stop_after():
            await asyncio.sleep(0.6)
            agent.stop()
        asyncio.create_task(stop_after())

        await asyncio.wait_for(agent.run(), timeout=2.0)

        # 验证 session 创建
        s_list = agent.sessions.list_all()
        assert len(s_list) == 1
        # CLI 被调
        assert len(env["cli"].calls) >= 1
        # 频道有 reply
        ch = agent.channel("general")
        msgs = ch.tail(5)
        reply_msgs = [m for m in msgs if m["from"] == "qwencode"]
        assert len(reply_msgs) >= 1
        assert "100 元" in reply_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_stop_breaks_loop(self, env):
        """stop() 后 run() 退出."""
        agent = env["agent"]

        async def stop_after():
            await asyncio.sleep(0.2)
            agent.stop()
        asyncio.create_task(stop_after())

        # 不超时 = 正确退出
        await asyncio.wait_for(agent.run(), timeout=2.0)
        # 验证 comms 停了
        assert agent.comms._stop_event.is_set()


class TestAgentWorkspace:
    def test_workspace_md_includes_role(self, tmp_path):
        agent = Agent(
            agent_id="seller", cli=MockCLI(), data_dir=tmp_path,
            system_prompt="你是卖鱼的, 开价 100",
        )
        content = (agent.workspace_dir / "mock.md").read_text()
        assert "seller" in content
        assert "开价 100" in content

    def test_workspace_md_includes_capabilities(self, tmp_path):
        agent = Agent(
            agent_id="seller", cli=MockCLI(), data_dir=tmp_path,
            capabilities=["python", "go", "rust"],
        )
        content = (agent.workspace_dir / "mock.md").read_text()
        assert "python" in content
        assert "go" in content
        assert "rust" in content

    def test_workspace_md_different_cli_names(self, tmp_path):
        # MockCLI name="mock"
        a1 = Agent(agent_id="a1", cli=MockCLI(), data_dir=tmp_path)
        assert (a1.workspace_dir / "mock.md").exists()
        # 不同 CLI -> 不同 MD 文件名
        from agents_chat.infra.cli import OpenCodeCLI
        a2 = Agent(agent_id="a2", cli=OpenCodeCLI(), data_dir=tmp_path)
        assert (a2.workspace_dir / "opencode.md").exists()

    def test_workspace_md_not_overwritten(self, tmp_path):
        agent = Agent(agent_id="a", cli=MockCLI(), data_dir=tmp_path)
        md_path = agent.workspace_dir / "mock.md"
        original = md_path.read_text()
        md_path.write_text("# 我手动改的\n")
        # 重新构造 Agent
        agent2 = Agent(agent_id="a", cli=MockCLI(), data_dir=tmp_path, workspace_dir=agent.workspace_dir)
        assert md_path.read_text() == "# 我手动改的\n"


class TestAgentCLIIntegration:
    @pytest.mark.asyncio
    async def test_full_bargain_flow_mock_cli(self, env):
        """完整流程: 1 个 agent + mock CLI, 收 mention → 处理 → 写频道."""
        agent = env["agent"]
        env["cli"].output = "100 元一斤"
        env["cli"].progress = 10
        env["cli"].summary = "开价 100"

        # 投递 mention
        agent.mailbox.append(
            ref_msg_id="ch_1", type="mention",
            content="@qwencode 鱼怎么卖", channel="general",
        )
        # 手动跑一次 scheduler.handle_mail
        mails = agent.mailbox.read_and_clear()
        await agent.scheduler.handle_mail(mails[0])

        # 验证
        s = agent.sessions.list_all()[0]
        assert s.topic == "鱼怎么卖"
        assert s.progress == 10
        # 频道
        ch = agent.channel("general")
        reply = [m for m in ch.tail(5) if m["from"] == "qwencode"][0]
        assert "100 元" in reply["content"]
        # STATUS 块应在
        assert "<!--STATUS" in reply["content"]
